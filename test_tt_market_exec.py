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
        from datetime import datetime, timedelta, timezone
        self.event = {"eventId": "100", "player1": "One", "player2": "Two",
                      "receivedAt": datetime.now(timezone.utc).isoformat()}
        self.market = {"name": "Game 2", "marketId": "200", "eventId": "100"}
        self.plan = {"betType": "split", "selection": "Two", "selectionId": "300", "scoreState": [1, 0],
                     "nextSet": 2, "specification": {"family": "split", "player": 1, "target": 2},
                     "min_decimal": 1.85, "stake_cents": 261,
                     "deadline": (datetime.now(timezone.utc)+timedelta(seconds=30)).isoformat(), "display": {}}
        self.outcome = {"selectionId": "300", "label": "Two", "decimal": 2.5}
        self.quote = {"status": "armed", "targetFingerprint": "exact-leg-quote", "balance": {"availableBalance": 100.0}, "decimalOdds": 2.5,
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
                                  ("read_plan_slip", {"status": "empty", "stagingSafe": True, "legs": []}),
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
            if script is execution.PLAN_CLEAR_STAKE_SCRIPT:
                return {"cleared": True}
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
        self.assertEqual(sum(c.args[0] is execution.CONFIRM_PLAN_SCRIPT for c in driver.execute_script.call_args_list), 1)
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
        self.assertEqual([c.args[0] for c in driver.execute_script.call_args_list],
                         [execution.CONFIRM_PLAN_SCRIPT, execution.PLAN_CLEAR_STAKE_SCRIPT])

    def test_repeated_resizes_stop_fail_closed(self):
        confirm_results = iter([{"status": "resize"}] * 3)
        driver = self.scripted_driver(confirm_results, arms=[self.armed] * 3, quotes=[self.quote] * 3)
        result = self.run_plan(driver, lambda quote: {"stake": 2.61, "min_decimal": 1.85, "echo": "e1"})
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(sum(c.args[0] is execution.CONFIRM_PLAN_SCRIPT for c in driver.execute_script.call_args_list), 3)


class SlipFirstOrderTests(unittest.TestCase):
    setUp = PlanExecutionTests.setUp

    def hot(self, denial=False, failure=None, fatal_at=None, continuation=False,
             quote_failure=None, snapshot=None, cleanup=True, deadline_budget=None, receipt_error=None,
              closed=False, removal_status='cleared',observe_price=None):
        from contextlib import ExitStack
        from unittest.mock import patch
        self.order, self.configs = [], []
        driver = self.driver = Mock()
        identity = {"eventId": "100", "marketId": "200", "selectionId": "300"}
        closed_removed = single_removed = False

        def record(name, value):
            def call(*args, **kwargs):
                self.order.append(name)
                if name == fatal_at:
                    raise execution.DriverConnectionLost("synthetic driver loss")
                return value
            return call

        def dispatch(script, *args):
            nonlocal closed_removed,single_removed
            if script is execution.PLAN_SLIP_SNAPSHOT_SCRIPT:
                return record("snapshot", {"status": "present", "stagingSafe": True,
                                           "legs": [identity] if single_removed else [identity, {**identity, "selectionId": "other",'closed':closed and not closed_removed}]} if snapshot is None else snapshot)()
            if script is execution.PLAN_SINGLE_SLIP_SCRIPT:
                single_removed=removal_status=='cleared'
                return record('single_remove',{'status':removal_status})()
            if script is execution.PLAN_REMOVE_CLOSED_SCRIPT:
                closed_removed = removal_status == 'cleared'
                return record('closed_remove',{'status':removal_status})()
            if script is execution.WAIT_FOR_PLAN_ARM_SCRIPT:
                self.configs.append(args[0])
                return record("quote" if args[0].get("readOnly") else "arm",
                              quote_failure if args[0].get("readOnly") and quote_failure is not None
                              else {**self.quote, "targetFingerprint": "exact-leg-quote"})()
            if script is execution.CONFIRM_PLAN_SCRIPT:
                self.configs.append(args[0])
                return record("confirm", failure or {"status": "submitted", "decimalOdds": 2.5})()
            if script is execution.PLAN_CLEAR_STAKE_SCRIPT:
                self.configs.append(args[0])
                return record("cleanup", {"cleared": cleanup})()
            self.fail("unexpected script")

        def receipt(driver, confirmed, script, continue_submit):
            record("receipt", None)()
            if receipt_error is not None:
                raise receipt_error
            if continuation:
                continued = continue_submit({"kind": "accepted-price", "token": "t", "fingerprint": "f"})
                self.assertEqual(continued["status"], "submitted")
            return {"placed": True, "lastConfirmation": confirmed, "text": "Bet placed"}

        driver.execute_script.side_effect = driver.execute_async_script.side_effect = dispatch
        with ExitStack() as stack:
            for name, value in (("signed_in_state", "signed-in"), ("location_status", {"blocked": False}),
                                ("recover_load_notice", True), ("ensure_slip_open", True)):
                stack.enter_context(patch.object(execution, name, side_effect=record(name, value)))
            stack.enter_context(patch.object(execution, "settle_submission", side_effect=receipt))
            stack.enter_context(patch.object(execution, "save_slip_capture"))
            for name in ("clear_slip", "click_selection", "correct_event", "open_props"):
                stack.enter_context(patch.object(execution, name, side_effect=AssertionError("board path used")))
            return execution.place_plan(driver, self.event, self.market, self.plan, self.outcome,
                authorize_values=record("claim", {"denied": "signal_expired"} if denial else
                                        {"stake": 2.61, "min_decimal": 1.85}),
                validate=record("validate", True), deadline_budget=deadline_budget,observe_price=observe_price)

    def test_reference_plan_cleans_other_unfunded_picks_before_read_claim_arm_submit(self):
        self.plan['window']={};self.plan['min_decimal']=2.5
        self.assertEqual(self.hot()['status'],'placed')
        self.assertEqual(self.order,['validate','signed_in_state','location_status','recover_load_notice',
            'ensure_slip_open','snapshot','single_remove','snapshot','quote','claim','arm','validate','confirm','receipt'])
        for status in ('waiting','blocked',None):
            with self.subTest(status=status):
                result=self.hot(removal_status=status)
                self.assertEqual(result['submissionState'],'not-submitted')
                self.assertNotIn('claim',self.order)
                self.assertNotIn('arm',self.order)
                self.assertNotIn('confirm',self.order)

    def test_observed_decline_stops_before_claim_or_stake(self):
        self.plan['window']={};self.plan['min_decimal']=2.5
        observer=Mock(return_value=False)
        result=self.hot(observe_price=observer)
        observer.assert_called_once_with(2.5)
        self.assertTrue(result['windowExpired'])
        self.assertEqual(result['submissionState'],'not-submitted')
        self.assertEqual(self.order[-1],'quote')
        self.assertNotIn('claim',self.order)

    def test_exact_staged_leg_has_no_board_dependency_and_one_submit(self):
        self.assertEqual(self.hot()["status"], "placed")
        self.assertEqual(self.order, ["validate", "signed_in_state", "location_status", "recover_load_notice",
            "ensure_slip_open", "snapshot", "quote", "claim", "arm", "validate", "confirm", "receipt"])
        self.assertTrue(all(0 < cfg['timeoutMs'] <= 250 for cfg in self.configs if 'timeoutMs' in cfg))
        for config in self.configs:
            for key, value in (("eventId", "100"), ("marketId", "200"), ("selectionId", "300"),
                               ("selectionLabel", "Two"), ("marketName", "Game 2"), ("selectionLine", None)):
                self.assertEqual(config[key], value)
        self.assertEqual(self.configs[-1]["expectedFingerprint"], "exact-leg-quote")
        self.assertEqual(self.configs[-1]["expectedDecimal"], 2.5)

    def test_denial_stops_after_claim_without_stake_or_submit(self):
        self.assertEqual(self.hot(denial=True)["denied"], "signal_expired")
        self.assertEqual(self.order[-2:], ["quote", "claim"])
        self.assertNotIn("arm", self.order)
        self.assertNotIn("cleanup", self.order)

    def test_closed_removal_is_verified_before_quote_claim_and_stake(self):
        self.assertEqual(self.hot(closed=True)['status'],'placed')
        self.assertEqual(self.order,['validate','signed_in_state','location_status','recover_load_notice',
            'ensure_slip_open','snapshot','closed_remove','snapshot','quote','claim','arm','validate','confirm','receipt'])

    def test_unverified_or_lost_closed_removal_never_claims_or_submits(self):
        for status in ('waiting','blocked',None):
            with self.subTest(status=status):
                result=self.hot(closed=True,removal_status=status)
                self.assertTrue(result['retryable'])
                self.assertEqual(result['submissionState'],'not-submitted')
                self.assertEqual(self.order[-1],'closed_remove')
        with self.assertRaises(execution.DriverConnectionLost):
            self.hot(closed=True,fatal_at='closed_remove')
        self.assertEqual(self.order[-1],'closed_remove')
        calls=len(self.driver.mock_calls)
        self.assertEqual(execution.remove_closed_picks(self.driver)['status'],'blocked')
        self.assertEqual(len(self.driver.mock_calls),calls)

    def test_uncertain_submission_never_dispatches_closed_pick_removal(self):
        driver=Mock();driver._tt_plan_uncertain=True
        self.assertEqual(execution.remove_closed_picks(driver)['status'],'blocked')
        driver.execute_script.assert_not_called()

    def test_definite_abort_clears_only_owned_target_after_confirm(self):
        result = self.hot(failure={"status": "aborted", "uiRejected": True, "reason": "changed line"})
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(self.order[-2:], ["confirm", "cleanup"])

    def test_driver_loss_at_each_money_path_stage_is_never_retried_or_cleaned(self):
        for stage in ("quote", "arm", "confirm", "receipt"):
            with self.subTest(stage=stage), self.assertRaises(execution.DriverConnectionLost):
                self.hot(fatal_at=stage)
            self.assertEqual(self.order[-1], stage)
            self.assertNotIn("cleanup", self.order)

    def test_unknown_confirm_response_latches_uncertainty_without_cleanup(self):
        result = self.hot(failure={"unknown": True})
        self.assertEqual(result["status"], "unconfirmed")
        self.assertTrue(result["submitted"])
        self.assertEqual(self.order[-1], "confirm")
        count = len(self.driver.mock_calls)
        second = execution.place_plan(self.driver, self.event, self.market, self.plan, self.outcome, Mock())
        self.assertEqual(second["status"], "stopped")
        self.assertEqual(len(self.driver.mock_calls), count)

    def test_continuation_retains_exact_identity_and_claimed_price(self):
        self.assertEqual(self.hot(continuation=True)["status"], "placed")
        self.assertEqual(self.order[-4:], ["validate", "quote", "claim", "confirm"])
        self.assertEqual(self.order.count('arm'), 1)
        for config in self.configs[1:]:
            self.assertEqual(config["expectedFingerprint"], "exact-leg-quote")
            self.assertEqual(config["selectionId"], "300")
            self.assertEqual(config["eventId"], "100")
            self.assertEqual(config["marketId"], "200")

    def test_suspended_or_unready_quote_yields_before_claim_and_money(self):
        for quote in ({'status':'aborted','notReady':True,'reason':'target suspended'},
                      {'status':'waiting','empty':True,'reason':'target absent'}):
            with self.subTest(quote=quote):
                result = self.hot(quote_failure=quote)
                self.assertEqual(result['submissionState'], 'not-submitted')
                self.assertTrue(result['retryable'])
                self.assertFalse(result['submitted'])
                self.assertEqual(self.order[-1], 'quote')

    def test_absent_target_in_populated_slip_yields_without_board_or_quote_wait(self):
        result = self.hot(snapshot={'status':'present','stagingSafe':True,
                                   'legs':[{'eventId':'101','marketId':'200','selectionId':'301'}]})
        self.assertTrue(result['notReady'])
        self.assertTrue(result['retryable'])
        self.assertEqual(result['submissionState'], 'not-submitted')
        self.assertEqual(self.order[-1], 'snapshot')

    def test_invalid_or_exhausted_dispatch_budget_never_claims_or_clicks(self):
        for budget in (0, -1, float('nan'), float('inf'), True, '1'):
            with self.subTest(budget=budget):
                result = self.hot(deadline_budget=budget)
                self.assertEqual(result['submissionState'], 'not-submitted')
                self.assertFalse(result['retryable'])
                self.assertNotIn('claim', self.order)
                self.assertNotIn('confirm', self.order)

    def test_cleanup_failure_stops_following_signals_without_driver_commands(self):
        result = self.hot(failure={'status':'aborted','uiRejected':True,'reason':'changed line'}, cleanup=False)
        self.assertEqual(result['status'], 'stopped')
        self.assertTrue(result['cleanupFailed'])
        self.assertEqual(result['submissionState'], 'not-submitted')
        calls = len(self.driver.mock_calls)
        self.assertEqual(execution.place_plan(self.driver, self.event, self.market, self.plan, self.outcome, Mock())['status'], 'stopped')
        self.assertEqual(len(self.driver.mock_calls), calls)


    def test_lost_driver_before_submission_latches_stop_for_next_signal_and_readiness(self):
        with self.assertRaises(execution.DriverConnectionLost):
            self.hot(fatal_at='quote')
        calls = len(self.driver.mock_calls)
        self.assertFalse(execution.plan_slip_ready(self.driver, self.event, self.market, self.plan))
        self.assertEqual(execution.place_plan(self.driver, self.event, self.market, self.plan, self.outcome, Mock())['status'], 'stopped')
        self.assertEqual(len(self.driver.mock_calls), calls)


    def test_post_submit_errors_cannot_be_reported_as_unsubmitted_or_cleaned(self):
        for error in (execution.SignInRequired('signed out'), execution.LocationRequired('location blocked'),
                      execution.BetNotSubmitted('callback expired'), execution.WebDriverException('reply lost')):
            with self.subTest(error=type(error).__name__):
                result = self.hot(receipt_error=error)
                self.assertEqual(result['status'], 'unconfirmed')
                self.assertEqual(result['submissionState'], 'uncertain')
                self.assertTrue(result['submitted'])
                self.assertFalse(result['retryable'])
                self.assertEqual(self.order[-1], 'receipt')
                self.assertNotIn('cleanup', self.order)


class OverlappingH2HOrderTests(unittest.TestCase):
    def test_deferring_suspended_or_absent_a_dispatches_b_before_rechecking_a(self):
        from contextlib import ExitStack
        from unittest.mock import patch
        from test_ember_slip import h2h_contract
        for absent in (False, True):
            with self.subTest(absent=absent), ExitStack() as stack:
                order, driver = [], Mock()
                waiting = True
                contracts = [h2h_contract(i) for i in range(2)]
                def dispatch(script, *args):
                    if script is execution.PLAN_SLIP_SNAPSHOT_SCRIPT:
                        legs = [{'eventId':event['eventId'], 'marketId':market['marketId'], 'selectionId':outcome['selectionId']}
                                for event, market, plan, outcome in contracts]
                        return {'status':'present', 'stagingSafe':True, 'legs':legs[1:] if waiting and absent else legs}
                    config = args[0]
                    identifier = config['eventId']
                    if script is execution.WAIT_FOR_PLAN_ARM_SCRIPT:
                        stage = 'quote' if config.get('readOnly') else 'arm'
                        order.append((identifier, stage))
                        if waiting and identifier == '10':
                            return {'status':'aborted', 'notReady':True, 'reason':'suspended A'}
                        self.assertLessEqual(config['timeoutMs'], 250)
                        return {'status':'armed', 'targetFingerprint':'h2h-'+identifier, 'decimalOdds':2.5,
                                'balance':{'availableBalance':100}, 'placeText':'Place Bet', 'placeDisabled':False,
                                'slipText':'Tomasz Lojtek Moneyline', 'odds':['+150']}
                    self.assertIs(script, execution.CONFIRM_PLAN_SCRIPT)
                    order.append((identifier, 'submit'))
                    return {'status':'submitted', 'decimalOdds':2.5}
                driver.execute_script.side_effect = driver.execute_async_script.side_effect = dispatch
                for name, value in (('signed_in_state','signed-in'), ('location_status',{'blocked':False}),
                                    ('recover_load_notice',True), ('ensure_slip_open',True), ('save_slip_capture',None)):
                    stack.enter_context(patch.object(execution, name, return_value=value))
                for name in ('click_selection','clear_slip','open_props','correct_event'):
                    stack.enter_context(patch.object(execution, name, side_effect=AssertionError('board dependency')))
                stack.enter_context(patch.object(execution, 'settle_submission',
                    side_effect=lambda d,c,*args: {'placed':True,'lastConfirmation':c,'text':'Bet placed'}))
                def run(index):
                    def claim(quote):
                        order.append((contracts[index][0]['eventId'], 'claim'))
                        return {'stake':2.61,'min_decimal':2.0}
                    return execution.place_plan(driver, *contracts[index], claim, deadline_budget=.5)
                a = run(0)
                self.assertTrue(a['retryable'])
                self.assertEqual(a['submissionState'], 'not-submitted')
                self.assertEqual(run(1)['submissionState'], 'placed')
                self.assertNotIn(('10','claim'), order)
                self.assertEqual([row for row in order if row[1]=='submit'], [('11','submit')])
                waiting = False
                self.assertEqual(run(0)['submissionState'], 'placed')
                self.assertEqual(order, ([] if absent else [('10','quote')]) +
                    [('11',stage) for stage in ('quote','claim','arm','submit')] +
                    [('10',stage) for stage in ('quote','claim','arm','submit')])


if __name__ == "__main__":
    unittest.main()
