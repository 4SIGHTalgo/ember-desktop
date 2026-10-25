"""Direct tests for the shared executor module (browser-free pieces + contracts)."""
import inspect
from unittest.mock import Mock

import unittest

import tt_market_exec as execution
from execution_policy import MARKET_FAMILIES


class ExecutorContractTests(unittest.TestCase):
    def test_the_executor_carries_no_strategy_imports(self):
        source = inspect.getsource(execution)
        for banned in ("bet_value", "tabletennistotals", "frozen_h2h", "import tt_elite_follow",
                       "from tt_elite_follow", "distribution_policy import contexts", "expected_value",
                       "\nCONFIRM_SCRIPT = ", "const halfKellySizing"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, source)

    def test_the_plan_confirm_is_composed_of_shared_reader_fragments_only(self):
        for fragment in ("LOCATION_READER_JS", "VISIBLE_UI_JS", "BALANCE_READER_JS", "SUBMISSION_UI_JS"):
            with self.subTest(fragment=fragment):
                self.assertIn(execution.__dict__[fragment], execution.CONFIRM_PLAN_SCRIPT)

    def test_reveal_takes_a_resolved_display_not_a_strategy_decision(self):
        signature = str(inspect.signature(execution.reveal_set_winner))
        self.assertIn("display", signature)
        self.assertNotIn("decision", signature)


class PureHelperTests(unittest.TestCase):
    def test_surface_family_maps_distribution_families_to_their_surface(self):
        self.assertEqual(execution.surface_family("split"), "split")
        self.assertEqual(execution.surface_family("complement"), "split")
        self.assertEqual(execution.surface_family("closeout"), "closeout")
        self.assertEqual(execution.surface_family("set_handicap"), "handicap")
        self.assertEqual(execution.surface_family("point_handicap"), "handicap")

    def test_market_surface_names_the_distribution_sections(self):
        for name in MARKET_FAMILIES:
            with self.subTest(name=name):
                surface = execution.market_surface({"name": name, "betType": MARKET_FAMILIES[name]})
                self.assertTrue(surface["distribution"])
                self.assertTrue(surface["sectionTitles"])

    def test_market_surface_defaults_to_the_winner_listed_game_section(self):
        surface = execution.market_surface({"name": "Game 2", "betType": "split"})
        self.assertEqual(surface["sectionTitle"], "Winner - Listed Game")
        self.assertFalse(surface["distribution"])

    def test_event_route_open_requires_the_exact_event_path(self):
        driver = Mock()
        driver.current_url = "https://sportsbook.draftkings.com/event/piotr-vs-tomasz/10"
        self.assertTrue(execution.event_route_open(driver, {"eventId": "10"}))
        driver.current_url = "https://sportsbook.draftkings.com/event/piotr-vs-tomasz/11"
        self.assertFalse(execution.event_route_open(driver, {"eventId": "10"}))
        driver.current_url = None
        self.assertFalse(execution.event_route_open(driver, {"eventId": "10"}))

    def test_keep_alive_line_reports_each_verdict(self):
        self.assertIsNone(execution.keep_alive_line(None))
        self.assertIn("reached the sportsbook", execution.keep_alive_line({"ok": True, "status": 200}))
        self.assertIn("answered HTTP 500", execution.keep_alive_line({"ok": False, "status": 500}))
        self.assertIn("did not reach", execution.keep_alive_line({"ok": False, "status": None}))

    def test_slip_capture_lives_in_the_executor_namespace(self):
        # The client rebinds this Path at runtime; only ownership is invariant.
        from pathlib import Path as _Path
        self.assertIsInstance(execution.SLIP_CAPTURE, _Path)


class PlanExecutionTests(unittest.TestCase):
    """Mock-driver tier: the plan flow's exact script-call order and denials."""

    def setUp(self):
        from datetime import datetime, timezone
        self.event = {"eventId": "100", "player1": "One", "player2": "Two",
                      "receivedAt": datetime.now(timezone.utc).isoformat()}
        self.market = {"name": "Game 2", "marketId": "200", "eventId": "100"}
        self.plan = {"betType": "split", "selection": "Two", "selectionId": "300", "scoreState": [1, 0],
                     "nextSet": 2, "specification": {"family": "split", "player": 1, "target": 2},
                     "min_decimal": 1.85, "stake_cents": 261,
                     "deadline": datetime.now(timezone.utc).isoformat(), "display": {}}
        self.outcome = {"selectionId": "300", "label": "Two", "decimal": 2.5}
        self.quote = {"status": "armed", "balance": {"availableBalance": 100.0}, "decimalOdds": 2.5,
                      "placeText": "Place Bet", "slipText": "One Two Game 2 2.00", "placeDisabled": False,
                      "acceptsOddsChange": False, "odds": ["+150"]}
        self.armed = {"status": "armed", "placeText": "Place Bet", "slipText": "One Two Game 2 2.00",
                      "placeDisabled": False, "acceptsOddsChange": False, "stakeValue": 2.61,
                      "decimalOdds": 2.5, "odds": ["+150"]}

    def run_plan(self, driver, authorize_values):
        from unittest.mock import patch
        from contextlib import ExitStack
        with ExitStack() as stack:
            for target, value in (("signed_in_state", "signed-in"),
                                  ("location_status", {"state": "ok", "blocked": False}),
                                  ("recover_load_notice", True),
                                  ("ensure_slip_open", True),
                                  ("read_slip_legs", {"status": "empty", "legs": []}),
                                  ("clear_slip", True),
                                  ("click_selection", {"status": "clicked"}),
                                  ("save_slip_capture", None),
                                  ("settle_submission", {"placed": True, "lastConfirmation":
                                          {"status": "submitted", "decimalOdds": 2.5, "submitKind": "place-bet"},
                                          "text": "Bet Placed", "actions": []})):
                stack.enter_context(patch.object(execution, target, return_value=value))
            return execution.place_plan(driver, self.event, self.market, self.plan, self.outcome,
                                        authorize_values=authorize_values)

    def scripted_driver(self, confirm_results, quotes=None, arms=None):
        driver = Mock()
        quotes = iter(quotes or [self.quote])
        arms = iter(arms or [self.armed])

        def async_dispatch(script, *args):
            if script is execution.WAIT_FOR_PLAN_ARM_SCRIPT:
                return (next(quotes) if args[0].get("readOnly") else next(arms)) or {}
            return {}

        def dispatch(script, *args):
            if script is execution.CONFIRM_PLAN_SCRIPT:
                return next(confirm_results) or {}
            return {}
        driver.execute_async_script.side_effect = async_dispatch
        driver.execute_script.side_effect = dispatch
        return driver

    def test_plan_flow_reads_authorizes_arms_and_compares_before_one_submit(self):
        authorized = []
        confirm_results = iter([{"status": "submitted", "decimalOdds": 2.5, "odds": ["+150"],
                                 "submitKind": "place-bet", "echo": "e1", "stake": 2.61}])

        def authorize_values(quote):
            authorized.append(quote)
            return {"stake": 2.61, "min_decimal": 1.85, "echo": "e1"}

        driver = self.scripted_driver(confirm_results)
        result = self.run_plan(driver, authorize_values)
        self.assertEqual(result["status"], "placed")
        self.assertEqual(result["stake"], 2.61)
        self.assertEqual(result["sizingMode"], "server_plan")
        self.assertEqual(authorized, [self.quote])
        self.assertEqual(driver.execute_script.call_count, 1)
        confirm_args = driver.execute_script.call_args[0]
        self.assertEqual(confirm_args[0], execution.CONFIRM_PLAN_SCRIPT)
        self.assertEqual(confirm_args[1]["stake"], 2.61)
        self.assertEqual(confirm_args[1]["minDecimal"], 1.85)
        self.assertEqual(confirm_args[1]["echo"], "e1")
        self.assertEqual(confirm_args[1]["selectionLabel"], "Two")
        self.assertEqual(confirm_args[1]["marketName"], "Game 2")

    def test_server_denial_skips_the_wager_without_touching_the_submit(self):
        driver = self.scripted_driver(iter([{"status": "submitted"}]))
        result = self.run_plan(driver, lambda quote: {"denied": "signal_expired"})
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["denied"], "signal_expired")
        self.assertFalse(result["submitted"])
        driver.execute_script.assert_not_called()

    def test_invalid_authorized_values_are_a_denial_not_a_bet(self):
        for values in (None, {"stake": 0}, {"stake": 2.61}, {"stake": "2.61", "min_decimal": 1.85},
                       {"stake": 2.61, "min_decimal": 1}):
            with self.subTest(values=values):
                driver = self.scripted_driver(iter([{"status": "submitted"}]))
                result = self.run_plan(driver, (lambda quote: values))
                self.assertEqual(result.get("status"), "skipped")
                self.assertEqual(result.get("denied"), "authorization_denied")
                driver.execute_script.assert_not_called()

    def test_a_changed_stake_re_authorizes_before_resubmitting(self):
        rounds = []

        def authorize_values(quote):
            rounds.append(quote)
            return {"stake": 2.61 if len(rounds) == 1 else 1.30, "min_decimal": 1.85, "echo": "e2"}

        confirm_results = iter([{"status": "resize", "reason": "the authorized stake changed on the slip"},
                                {"status": "submitted", "decimalOdds": 2.5, "submitKind": "place-bet"}])
        driver = self.scripted_driver(confirm_results,
                                      quotes=[self.quote] * 2,
                                      arms=[self.armed, {**self.armed, "stakeValue": 1.30}])
        result = self.run_plan(driver, authorize_values)
        self.assertEqual(result["status"], "placed")
        self.assertEqual(result["stake"], 1.30)
        self.assertEqual(len(rounds), 2)

    def test_a_price_below_the_floor_never_submits_and_reports_the_floor(self):
        confirm_results = iter([{"status": "aborted", "edgeRejected": True, "decimalOdds": 1.5,
                                 "minDecimal": 1.85, "odds": ["-200"],
                                 "reason": "the final slip price is below the authorized price floor"},
                                {"status": "submitted"}])
        driver = self.scripted_driver(confirm_results)
        result = self.run_plan(driver, lambda quote: {"stake": 2.61, "min_decimal": 1.85, "echo": "e1"})
        self.assertEqual(result["status"], "skipped")
        self.assertIn("price floor", result["reason"])
        self.assertEqual(result["comparison"]["slipDecimal"], 1.5)
        self.assertEqual(driver.execute_script.call_count, 1)

    def test_repeated_resizes_stop_fail_closed(self):
        confirm_results = iter([{"status": "resize"}] * 3)
        driver = self.scripted_driver(confirm_results, arms=[self.armed] * 3, quotes=[self.quote] * 3)
        result = self.run_plan(driver, lambda quote: {"stake": 2.61, "min_decimal": 1.85, "echo": "e1"})
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(driver.execute_script.call_count, 3)


if __name__ == "__main__":
    unittest.main()
