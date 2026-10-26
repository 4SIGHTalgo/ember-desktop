from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from selenium.common.exceptions import InvalidSessionIdException, WebDriverException

from ember_browser_preparation import BrowserPreparation


class BrowserPreparationTests(unittest.TestCase):
    def setUp(self):
        self.clock = [0]
        self.events = []
        self.core = SimpleNamespace(LEAGUE_URL="https://sportsbook.draftkings.com/leagues/tabletennis/tt-elite-series",
            FATAL_BROWSER_ERRORS=(InvalidSessionIdException,), WebDriverException=WebDriverException,
            correct_event=Mock(return_value=True), open_props=Mock(return_value=True))
        self.driver = SimpleNamespace(current_url=self.core.LEAGUE_URL)
        def league(_):
            self.events.append("league")
            self.driver.current_url = self.core.LEAGUE_URL
        self.notify = Mock()
        self.preparation = BrowserPreparation(self.core, lambda: self.events.append("grace"), league,
                                              self.notify, clock=lambda: self.clock[0])
        self.hint = {"event": {"eventId": "100", "player1": "One", "player2": "Two"},
                     "market_hint": {"betType": "closeout"},
                     "expires_at": (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()}
        self.server_now = datetime.now(timezone.utc).timestamp()

    def step(self, hint=None, ready=True):
        self.preparation.step(self.driver, hint, ready, self.server_now)

    def test_standby_uses_the_league_match_list_not_the_generic_category(self):
        self.driver.current_url = "https://sportsbook.draftkings.com/sports/tabletennis"
        self.step()
        self.assertEqual(self.events, ["grace", "league"])
        self.assertEqual(self.driver.current_url, self.core.LEAGUE_URL)
        self.assertEqual(self.notify.call_args.kwargs["state"], "league_standby")

    def test_provider_load_error_prevents_navigation_and_warmup_until_rechecked(self):
        readiness = Mock()
        readiness.provider_ready.side_effect = [False, False, True]
        readiness.warm.return_value = {'status':'ready'}
        self.preparation.market_readiness = readiness
        self.step(self.hint)
        self.step(self.hint)
        self.assertEqual(self.notify.call_args.kwargs['state'],'provider_loading')
        self.assertEqual(self.events,[])
        self.core.open_props.assert_not_called()
        readiness.warm.assert_not_called()
        self.step(self.hint)
        self.assertEqual(self.events,['grace'])
        self.core.open_props.assert_called_once()
        readiness.warm.assert_called_once()

    def test_candidate_navigation_marks_login_loading_grace_before_opening_event(self):
        self.core.open_props.side_effect = lambda *args, **kwargs: self.events.append("event") or True
        self.step(self.hint)
        self.assertEqual(self.events, ["grace", "event"])
        self.assertEqual(self.preparation.key, ("100", "closeout"))
        self.assertEqual(self.notify.call_args.kwargs["state"], "watching_event")

    def test_disconnect_pauses_preparation_and_reconnect_restores_the_target_status(self):
        self.step(self.hint)
        self.clock[0] = 3
        self.step(self.hint, ready=False)
        self.assertEqual(self.notify.call_args.kwargs["state"], "waiting_feed")
        self.step(self.hint)
        self.assertEqual(self.notify.call_args.kwargs["state"], "watching_event")
        self.assertEqual(self.core.open_props.call_count, 1)

    def test_unready_candidate_pages_have_a_bounded_retry_then_league_recovery(self):
        self.core.open_props.return_value = False
        for second in (0, 2, 4, 6, 8):
            self.clock[0] = second
            self.step(self.hint)
        self.assertEqual(self.core.open_props.call_count, 5)
        self.assertEqual(self.events[-2:], ["grace", "league"])
        self.assertEqual(self.notify.call_args.kwargs["state"], "event_retry")
        self.clock[0] = 9
        self.step(self.hint)
        self.assertEqual(self.core.open_props.call_count, 5)

    def test_a_retired_target_returns_to_the_league_after_a_short_snapshot_grace(self):
        self.step(self.hint)
        self.driver.current_url = "https://sportsbook.draftkings.com/event/one-vs-two/100"
        self.clock[0] = 3
        self.step()
        self.assertEqual(self.notify.call_args.kwargs["state"], "waiting_candidate")
        self.clock[0] = 7
        self.step()
        self.assertIsNone(self.preparation.key)
        self.assertEqual(self.driver.current_url, self.core.LEAGUE_URL)

    def test_prepared_target_continues_warming_instead_of_assuming_a_ready_page(self):
        readiness = Mock()
        readiness.warm.side_effect = [{"status": "waiting", "reason": "expanding winner section"}, {"status": "ready"}]
        self.preparation.market_readiness = readiness
        self.step(self.hint)
        self.assertEqual(self.notify.call_args.kwargs["state"], "preparing_market")
        self.clock[0] = 3
        self.step(self.hint)
        self.assertEqual(self.notify.call_args.kwargs["state"], "watching_event")
        self.assertEqual(readiness.warm.call_count, 2)
        self.assertEqual(self.core.open_props.call_count, 1)

    def test_staged_matches_or_live_board_hold_league_navigation_after_hint_expiry(self):
        self.step(self.hint)
        self.events.clear()
        self.clock[0] = 30
        self.preparation.step(self.driver, None, True, self.server_now, hold=True)
        self.assertEqual(self.events, [])
        self.assertEqual(self.preparation.key, ('100', 'closeout'))
        self.preparation.step(self.driver, self.hint, False, self.server_now, hold=True)
        self.assertEqual(self.events, [])

    def test_live_inventory_also_blocks_failed_event_league_recovery(self):
        self.core.open_props.return_value = False
        for second in (0, 2, 4, 6, 8):
            self.clock[0] = second
            self.preparation.step(self.driver, self.hint, True, self.server_now, hold=True)
        self.assertNotIn('league', self.events)

    def stage(self, event_id, bet_type="closeout", window=False, points=(5, 3)):
        now = datetime.now(timezone.utc)
        event = {"eventId": event_id, "player1": f"A{event_id}", "player2": f"B{event_id}",
                 "status": "STARTED", "period": "2nd", "setTally": [1, 0],
                 "sets": [{"set": 1, "home": 11, "away": 7}],
                 "currentSet": {"set": 2, "home": points[0] if not window else 0,
                                "away": points[1] if not window else 0}}
        return {"protocol": 1, "event_id": event_id, "bet_type": bet_type, "market_name": "Moneyline",
                "market_id": f"m{event_id}", "selection_id": f"s{event_id}", "label": f"A{event_id}",
                "line": None, "updated_at": now.isoformat(),
                "expires_at": (now+timedelta(seconds=10)).isoformat(), "event": event}

    def staged_leg(self, stage):
        return {"eventId": stage["event_id"], "marketId": stage["market_id"],
                "selectionId": stage["selection_id"]}

    def test_stage_catalog_rotates_the_focus_to_the_unstaged_match(self):
        # correct_event is only consulted once the focus is already on the page.
        self.core.correct_event = Mock(return_value=True)
        first, second = self.stage("100"), self.stage("101", bet_type="split")
        self.preparation.step(self.driver, self.hint, True, self.server_now,
                              stages=[first, second], legs=[])
        self.assertEqual(self.preparation.focus_event_id(), "100")
        self.core.open_props.assert_called_once()
        self.assertEqual(self.core.open_props.call_args.args[1]["eventId"], "100")
        self.assertEqual(self.core.open_props.call_args.kwargs["timeout"], 2.5)
        self.clock[0] = 2
        self.preparation.step(self.driver, self.hint, True, self.server_now,
                              stages=[first, second], legs=[self.staged_leg(first)])
        self.assertEqual(self.preparation.focus_event_id(), "101")
        self.assertEqual(self.core.open_props.call_args.args[1]["eventId"], "101")
        # Fully staged matches keep a sticky focus instead of oscillating.
        self.clock[0] = 4
        self.preparation.step(self.driver, self.hint, True, self.server_now,
                              stages=[first, second], legs=[self.staged_leg(first), self.staged_leg(second)])
        self.assertEqual(self.preparation.focus_event_id(), "101")
        self.assertEqual(self.core.open_props.call_count, 2)

    def test_an_open_entry_window_takes_the_focus_over_a_midset_target(self):
        self.core.correct_event = Mock(return_value=False)
        first, second = self.stage("100"), self.stage("101", bet_type="split")
        self.preparation.step(self.driver, self.hint, True, self.server_now,
                              stages=[first, second], legs=[])
        self.assertEqual(self.preparation.focus_event_id(), "100")
        opened = self.stage("101", window=True, bet_type="split")
        self.clock[0] = 2
        self.preparation.step(self.driver, self.hint, True, self.server_now,
                              stages=[first, opened], legs=[])
        self.assertEqual(self.preparation.focus_event_id(), "101")
        self.assertEqual(self.core.open_props.call_args.args[1]["eventId"], "101")

    def test_a_closer_unstaged_match_needs_a_clear_margin_to_take_the_focus(self):
        self.core.correct_event = Mock(return_value=False)
        first = self.stage("100", points=(5, 3))
        farther = self.stage("101", points=(4, 2), bet_type="split")
        self.preparation.step(self.driver, self.hint, True, self.server_now,
                              stages=[first, farther], legs=[])
        self.assertEqual(self.preparation.focus_event_id(), "100")
        nearer = self.stage("101", points=(6, 4), bet_type="split")
        self.clock[0] = 2
        # One point closer is inside the hysteresis margin; the focus holds.
        self.preparation.step(self.driver, self.hint, True, self.server_now,
                              stages=[first, nearer], legs=[])
        self.assertEqual(self.preparation.focus_event_id(), "100")
        closest = self.stage("101", points=(8, 6), bet_type="split")
        self.clock[0] = 4
        self.preparation.step(self.driver, self.hint, True, self.server_now,
                              stages=[first, closest], legs=[])
        self.assertEqual(self.preparation.focus_event_id(), "101")

    def test_expired_stages_fall_back_to_the_server_hint(self):
        self.core.correct_event = Mock(return_value=False)
        stale = self.stage("300")
        stale["expires_at"] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        self.preparation.step(self.driver, self.hint, True, self.server_now, stages=[stale], legs=[])
        self.assertEqual(self.preparation.focus_event_id(), "100")
        self.assertEqual(self.core.open_props.call_args.args[1]["eventId"], "100")

    def test_stage_navigation_keeps_the_bounded_retry_and_league_recovery(self):
        self.core.correct_event = Mock(return_value=False)
        self.core.open_props.return_value = False
        stages = [self.stage("100")]
        for second in (0, 2, 4, 6, 8):
            self.clock[0] = second
            self.preparation.step(self.driver, self.hint, True, self.server_now, stages=stages, legs=[])
        self.assertEqual(self.core.open_props.call_count, 5)
        self.assertEqual(self.events[-2:], ["grace", "league"])
        self.assertEqual(self.notify.call_args.kwargs["state"], "event_retry")
        self.clock[0] = 9
        self.preparation.step(self.driver, self.hint, True, self.server_now, stages=stages, legs=[])
        self.assertEqual(self.core.open_props.call_count, 5)


if __name__ == "__main__":
    unittest.main()
