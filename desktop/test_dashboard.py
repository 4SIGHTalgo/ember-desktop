"""Real-browser dashboard fixtures. No provider site, account, or wager actions."""
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
import tempfile
import threading
import unittest

from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from driver_lifecycle import create_owned_chrome
from test_ember_opportunity import make_snapshot


BRIDGE = """
window.dashboardCalls=[];
window.__TAURI__={core:{invoke:async(command,args)=>{
    window.dashboardCalls.push({command,args});
    if(command==='test_alert_sound')return true;
    if(command==='check_update')return window.dashboardUpdate || {available:false};
    return false;
}},event:{listen:async(name,callback)=>{window.dashboardWorker=callback;}}};
"""


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def handle(self):
        try:
            super().handle()
        except ConnectionResetError:
            pass


def board_event(identifier="101", names=None, reverse=False):
    now = datetime.now(timezone.utc).isoformat()
    return {"event_id": identifier, "players": names or ["Ada North", "Bea South"], "status": "STARTED",
            "set_tally": [0, 1] if reverse else [1, 0], "points": [4, 8] if reverse else [6, 4],
            "period": "2nd", "sets": [{"set": 1, "home": 7 if reverse else 11, "away": 11 if reverse else 7},
                                      {"set": 2, "home": 4 if reverse else 6, "away": 8 if reverse else 4}],
            "score_at": now, "markets": [{"market_id": "m"+identifier, "name": "Game 2",
                "outcomes": [{"selection_id": "s"+identifier, "label": (names or ["Ada North"])[0],
                              "line": None, "decimal_odds": 1.95, "conservative_ev": None if reverse else 12.5,
                              "entry_ev": 21.6 if reverse else 18.75, "entry_open": False, "quote_at": now}]}]}


def position(event, identifier, **snapshot):
    return {"id": identifier, "contract": event["event_id"]+":split", "outcome": "placed",
            "snapshot": {"event_id": event["event_id"], "players": event["players"], "market": "Game 2",
                         "selection": event["players"][0], "bet_type": "split", "target_game": 2,
                         "score_state": event["set_tally"], "decimal_odds": 1.95, "point_ev": 25,
                         **snapshot}}


class DashboardBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        handler = partial(QuietHandler, directory=str(Path(__file__).parent/"ui"))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        options = Options()
        options.add_argument("--headless=new")
        cls.driver = create_owned_chrome(options)
        cls.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": BRIDGE})
        cls.artifacts = tempfile.TemporaryDirectory(prefix="ember-dashboard-")

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.artifacts.cleanup()

    def setUp(self):
        self.driver.set_window_size(1040, 1000)
        self.driver.get(f"http://127.0.0.1:{self.server.server_port}/")
        WebDriverWait(self.driver, 5).until(lambda driver: driver.execute_script("return typeof window.dashboardWorker === 'function'"))

    def emit(self, **payload):
        self.driver.execute_script("window.dashboardWorker({payload:arguments[0]})", payload)

    def value(self, identifier):
        return self.driver.find_element("id", identifier).text

    def board(self, events, **overrides):
        now = datetime.now(timezone.utc)
        view = {"protocol": 1, "updated_at": now.isoformat(), "expires_at": (now+timedelta(seconds=10)).isoformat(),
                "events": events, **overrides}
        self.emit(kind="scanner", connected=True, healthy=True, server_offset=0, received_at=now.isoformat(),
                  snapshot={"board": view})
        return view

    def card(self, identifier):
        return self.driver.find_element("css selector", f'.match-card[data-event-id="{identifier}"]')

    def tracking(self, identifier):
        return self.driver.find_element("css selector", f'.position-row[data-position-id="{identifier}"]').get_attribute("data-tracking")

    def assert_no_start(self):
        self.assertFalse(self.driver.execute_script("return window.dashboardCalls.some(c=>c.command==='start')"))

    def show(self, decimal=2, closed=False, qualified=True):
        view, _, _ = make_snapshot(decimal, closed, qualified)
        now = datetime.now(timezone.utc)
        view.update(updated_at=now.isoformat(), expires_at=(now+timedelta(seconds=5)).isoformat(),
                    score_at=now.isoformat(), market_at=now.isoformat())
        self.emit(kind="scanner", connected=True, healthy=True, server_offset=0, received_at=now.isoformat(),
                  snapshot={"counts": {"events": 3, "dk_live": 2, "ready": 1, "qualified": int(qualified)},
                            "opportunity": view, "detail": "Local test fixture"})
        return view

    def test_real_decision_renders_percent_units_score_odds_and_conditional_history(self):
        view = self.show()
        self.assertEqual(self.value("opportunity-heading"), "Piotr Zemla vs Tomasz Lojtek")
        self.assertEqual(self.value("sets-one"), "1")
        self.assertEqual(self.value("point-score"), "2nd set · Current points 0 – 0")
        self.assertEqual(self.value("ev-value"), f"+{view['candidates'][0]['gate_ev']:.2f}%")
        self.assertIn("+20.00%", self.driver.find_element("id", "candidate-cards").text)
        self.assertIn("18 successes / 30 matching cases", self.driver.find_element("id", "candidate-cards").text)
        self.assertIn("2.00", self.value("market-name"))
        self.assertEqual(self.value("opportunity-stage"), "Qualified · execution stopped")

    def test_below_threshold_opportunity_is_visible_and_not_marked_ready(self):
        self.show(decimal=1.2, qualified=False)
        self.assertEqual(self.value("opportunity-stage"), "Below EV gate")
        self.assertIn("does not strictly exceed", self.value("decision-reason"))
        self.assertTrue(self.value("ev-value").startswith("−"))
        self.assertEqual(self.driver.find_element("id", "check-ev").get_attribute("data-passed"), "false")

    def test_visible_current_odds_are_distinguished_from_a_stale_verified_score(self):
        for stale_field,expected in (('score_at','Score stale'),('market_at','Quote stale')):
            with self.subTest(stale_field=stale_field):
                view=self.show()
                view[stale_field]=(datetime.now(timezone.utc)-timedelta(seconds=20)).isoformat()
                self.emit(kind='scanner',connected=True,healthy=True,server_offset=0,
                    received_at=datetime.now(timezone.utc).isoformat(),snapshot={'opportunity':view})
                self.assertEqual(self.value('opportunity-stage'),expected)
                self.assertIn('2.00',self.value('market-name'))
                self.assertIn('Quote last verified',self.value('decision-reason'))
                self.assertIn('score last verified',self.value('decision-reason'))
                self.assertEqual(self.value('ev-value'),'—')
                self.assert_no_start()

    def test_strategy_kelly_and_balance_percent_reach_native_start_and_persist(self):
        from selenium.webdriver.support.ui import Select
        self.emit(kind="signed_in", enabled=True, supported=True)
        Select(self.driver.find_element("id", "conservative-level")).select_by_value("conservative")
        Select(self.driver.find_element("id", "kelly")).select_by_value("0.5")
        cap = self.driver.find_element("id", "cap")
        cap.clear(); cap.send_keys("3")
        self.driver.find_element("id", "start").click()
        calls = self.driver.execute_script("return window.dashboardCalls.filter(c=>c.command==='start')")
        self.assertEqual(calls[-1]["args"], {"maxBankrollRiskPercent": 3, "conservativeLevel": "conservative", "kellyMultiplier": .5})
        self.emit(kind="session", running=True)
        self.assertFalse(cap.is_enabled())
        self.driver.refresh()
        self.assertEqual(self.driver.find_element("id", "cap").get_attribute("value"), "3")

    def test_closed_start_state_does_not_display_a_fabricated_zero_or_live_ev(self):
        self.show(closed=True, qualified=False)
        self.assertEqual(self.value("ev-value"), "—")
        self.assertEqual(self.value("opportunity-stage"), "Entry closed")
        self.assertIn("1 – 0", self.value("point-score"))

    def test_client_decorated_closed_window_shows_its_estimate_with_an_entry_closed_stage(self):
        view = self.show(closed=True, qualified=False)
        # When a later server ships computable estimates for closed windows, the
        # panel shows them while the entry window stays visibly non-executable.
        for row in view["candidates"]:
            row.update(conservative_ev=12.5, gate_ev=15.0)
        now = datetime.now(timezone.utc)
        self.emit(kind="scanner", connected=True, healthy=True, server_offset=0, received_at=now.isoformat(),
                  snapshot={"opportunity": view, "counts": {"events": 3, "dk_live": 1, "mapped": 1, "qualified": 0}})
        self.assertEqual(self.value("ev-value"), "+15.00%")
        self.assertEqual(self.value("opportunity-stage"), "Entry closed")
        self.assertEqual(self.driver.find_element("id", "check-window").get_attribute("data-passed"), "false")
        self.assertEqual(self.driver.find_element("id", "check-ev").get_attribute("data-passed"), "false")

    def test_board_status_names_live_matches_that_lack_verified_history(self):
        event = board_event()
        now = datetime.now(timezone.utc)
        view = {"protocol": 1, "updated_at": now.isoformat(), "expires_at": (now+timedelta(seconds=10)).isoformat(),
                "events": [event]}
        self.emit(kind="scanner", connected=True, healthy=True, server_offset=0, received_at=now.isoformat(),
                  snapshot={"board": view, "counts": {"dk_live": 2, "mapped": 1}})
        self.assertIn("DraftKings live 2 · 1 mapped", self.value("board-status"))
        self.emit(kind="scanner", connected=True, healthy=True, server_offset=0, received_at=now.isoformat(),
                  snapshot={"board": view, "counts": {"dk_live": 2, "mapped": 2}})
        self.assertNotIn("mapped", self.value("board-status"))

    def test_disconnect_marks_previous_values_as_stale(self):
        view = self.show()
        self.emit(kind="scanner", connected=False, healthy=False, received_at=datetime.now(timezone.utc).isoformat(),
                  snapshot={"opportunity": view})
        self.assertEqual(self.value("opportunity-stage"), "Data paused")
        self.assertEqual(self.driver.find_element("css selector", ".opportunity").get_attribute("data-stale"), "true")
        self.assertIn("last observation", self.value("decision-reason"))

    def test_browser_close_banner_survives_the_subsequent_stopped_event(self):
        self.show()
        self.emit(kind="alert", code="browser_closed", message="DraftKings browser closed. Monitoring stopped.")
        self.emit(kind="stopped", browser_closed=True)
        self.emit(kind="alert_sound", played=True)
        self.assertTrue(self.driver.find_element("id", "browser-alert").is_displayed())
        self.assertEqual(self.value("opportunity-stage"), "Browser closed")
        self.assertEqual(self.value("alert-sound"), "Audible notification played.")
        self.driver.find_element("id", "dismiss-alert").click()
        self.assertFalse(self.driver.find_element("id", "browser-alert").is_displayed())

    def test_normal_stop_has_no_close_alert_and_sound_test_uses_native_command(self):
        self.emit(kind="session", running=True)
        self.emit(kind="stopped", browser_closed=False)
        self.assertFalse(self.driver.find_element("id", "browser-alert").is_displayed())
        self.driver.find_element("id", "test-sound").click()
        self.assertIn("Sound played", self.value("test-sound"))
        calls = self.driver.execute_script("return window.dashboardCalls")
        self.assertEqual(sum(call["command"] == "test_alert_sound" for call in calls), 1)
        self.assertFalse(any(call["command"] == "start" for call in calls))

    def test_fault_recovery_pauses_permission_without_turning_into_a_manual_close(self):
        self.emit(kind="session", running=True)
        self.emit(kind="provider", ready=True)
        self.emit(kind="browser_recovery", state="recovering", message="Reconnecting the owned browser.")
        self.assertEqual(self.value("session-state"), "Recovering browser")
        self.assertFalse(self.driver.find_element("id", "browser-alert").is_displayed())
        self.emit(kind="browser_recovery", state="connected", message="Rechecking provider permission.")
        self.assertEqual(self.value("session-state"), "Waiting for DraftKings")
        self.emit(kind="provider", ready=True)
        self.assertEqual(self.value("session-state"), "Monitoring")
        self.emit(kind="stopped", browser_closed=False)
        self.assertEqual(self.value("session-state"), "Stopped")

    def test_recoverable_control_pipe_loss_preserves_the_running_intent(self):
        self.emit(kind="session", running=True)
        self.emit(kind="provider", ready=True)
        self.emit(kind="stopped", recoverable=True, browser_closed=False)
        self.assertEqual(self.value("session-state"), "Recovering browser")
        self.assertFalse(self.driver.find_element("id", "browser-alert").is_displayed())
        self.emit(kind="session", running=True)
        self.emit(kind="provider", ready=True)
        self.assertEqual(self.value("session-state"), "Monitoring")

    def test_participant_names_are_text_and_narrow_layout_does_not_overflow(self):
        view = self.show()
        view["players"][0] = '<img src=x onerror="window.bad=true">'
        now = datetime.now(timezone.utc).isoformat()
        self.emit(kind="scanner", connected=True, healthy=True, received_at=now, snapshot={"opportunity": view})
        self.assertIn("<img", self.value("opportunity-heading"))
        self.assertFalse(self.driver.execute_script("return window.bad === true"))
        for width in (580, 1040):
            self.driver.set_window_size(width, 900)
            self.assertTrue(self.driver.execute_script("return document.documentElement.scrollWidth <= innerWidth + 1"))

    def test_dashboard_visual_artifact(self):
        self.show()
        output = Path(self.artifacts.name)
        self.driver.save_screenshot(str(output/"opportunity-dashboard.png"))

    def test_missed_history_separates_uncertain_submissions_and_displays_sync_state(self):
        from test_ember_history import history_signal
        from ember_history import OpportunityJournal
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            journal = OpportunityJournal(Path(directory)/"history.sqlite3")
            scope = ("https://example.com", "1")
            missed, uncertain = history_signal(), history_signal(2, contract="200:split")
            journal.observe(scope, missed)
            journal.transition(scope, missed, "missed", "login_required")
            journal.observe(scope, uncertain)
            journal.transition(scope, uncertain, "uncertain", "submission_uncertain")
            self.emit(kind="missed_history", sync_state="retrying", **journal.snapshot(scope))
        self.assertEqual(self.value("missed-count"), "1")
        self.assertIn("2 records queued", self.value("history-sync"))
        self.assertIn("DraftKings sign-in was required", self.value("missed-list"))
        self.assertNotIn("check My Bets", self.value("missed-list"))
        self.driver.execute_script("arguments[0].scrollIntoView()", self.driver.find_element("css selector", ".missed-history"))
        output = Path(self.artifacts.name)
        self.driver.save_screenshot(str(output/"missed-bets-history.png"))
        from selenium.webdriver.support.ui import Select
        Select(self.driver.find_element("id", "history-filter")).select_by_value("uncertain")
        self.assertIn("Unconfirmed", self.value("missed-list"))
        self.assertIn("check My Bets", self.value("missed-list"))

    def test_sync_history_control_uses_only_the_reporting_command(self):
        self.emit(kind="signed_in", enabled=True, supported=True)
        self.driver.find_element("id", "sync-missed").click()
        calls = self.driver.execute_script("return window.dashboardCalls")
        self.assertTrue(any(call["command"] == "sync_missed_history" for call in calls))
        self.assertFalse(any(call["command"] == "start" for call in calls))

    def test_older_history_control_requests_an_account_scoped_page(self):
        self.emit(kind="missed_history", records=[], counts={"missed": 51}, queued=0, total=51,
                  offset=0, next_offset=50, sync_state="idle")
        self.driver.find_element("id", "history-older").click()
        calls = self.driver.execute_script("return window.dashboardCalls")
        self.assertEqual(calls[-1], {"command": "missed_history", "args": {"filter": "missed", "offset": 50}})


    def test_board_is_primary_and_keeps_multiple_events_even_for_the_same_h2h_pair(self):
        first, second = board_event(), board_event("202", reverse=True)
        self.board([first, second])
        self.assertEqual(self.value("board-count"), "2")
        self.assertIn("Sets 1 – 0", self.card("101").text)
        self.assertIn("Points 4 – 8", self.card("202").text)
        self.assertIn("Conservative EV +12.50%", self.card("101").text)
        self.assertIn("Conservative EV —", self.card("202").text)
        self.assertIn("Entry EV +21.60% · historical", self.card("202").text)
        self.assertNotIn("Conservative EV +21.60%", self.card("202").text)
        self.assertFalse(self.driver.find_element("id", "opportunity-diagnostics").get_attribute("open"))
        self.assertLess(self.driver.find_element("id", "board-heading").rect["y"], self.driver.find_element("css selector", ".diagnostics").rect["y"])
        self.assert_no_start()

    def test_missing_events_and_quotes_are_retained_but_cannot_show_live_ev(self):
        first, second = board_event(), board_event("202", ["Cy East", "Dee West"])
        self.board([first, second])
        self.board([second])
        self.assertIn("Stale · last observation", self.card("101").text)
        self.assertIn("Conservative EV —", self.card("101").text)
        self.assertIn("Conservative EV +12.50%", self.card("202").text)
        second["markets"][0]["outcomes"] = []
        self.board([second])
        self.assertIn("Conservative EV —", self.card("202").text)
        self.assertIn("1.95 · last", self.card("202").text)
        self.emit(kind="scanner", connected=True, healthy=True, received_at=datetime.now(timezone.utc).isoformat(), snapshot={})
        self.assertEqual(self.value("board-count"), "2")
        self.assertIn("Board unavailable / legacy snapshot", self.value("board-status"))
        self.assertNotIn("Conservative EV +", self.value("board-events"))
        self.assert_no_start()

    def test_disconnect_expiry_and_stale_score_or_quote_hide_ev(self):
        for case in ("disconnect", "expiry", "score", "quote", "version"):
            with self.subTest(case=case):
                event = board_event()
                old = (datetime.now(timezone.utc)-timedelta(seconds=20)).isoformat()
                if case == "score": event["score_at"] = old
                if case == "quote": event["markets"][0]["outcomes"][0]["quote_at"] = old
                view = self.board([event])
                if case == "disconnect":
                    self.emit(kind="scanner", connected=False, healthy=False, received_at=datetime.now(timezone.utc).isoformat(), snapshot={"board": view})
                elif case == "expiry": self.board([event], expires_at=old)
                elif case == "version": self.board([event], protocol=2)
                self.assertIn("Conservative EV —", self.card("101").text)
                self.assertIn("Entry EV +18.75% · historical", self.card("101").text)
        self.assert_no_start()

    def test_ev_expires_on_the_timer_without_a_worker_update(self):
        self.board([board_event()])
        self.assertIn("Conservative EV +12.50%", self.card("101").text)
        self.driver.execute_script("const now=Date.now(); Date.now=()=>now+31000;")
        WebDriverWait(self.driver, 4).until(lambda _: "Conservative EV —" in self.card("101").text)
        self.assertEqual(self.card("101").get_attribute("data-stale"), "true")

    def test_all_slip_legs_persist_until_fresh_verified_match_completion(self):
        first, second = board_event(), board_event("202", ["Cy East", "Dee West"])
        self.board([first, second])
        legs = [{"event_id": event["event_id"], "market_id": "m"+event["event_id"], "selection_id": str(i),
                 "label": event["players"][i], "market": "Game 2", "line": None, "decimal_odds": 1.95, "status": "staged"}
                for event in (first, second) for i in (0, 1)]
        self.emit(kind="slip", legs=legs, checked_at=datetime.now(timezone.utc).isoformat())
        self.assertEqual(len(self.driver.find_elements("css selector", ".staged-leg")), 4)
        self.emit(kind="slip", legs=[], checked_at=datetime.now(timezone.utc).isoformat())
        self.assertTrue(all(row.get_attribute("data-stale") == "true" for row in self.driver.find_elements("css selector", ".staged-leg")))
        self.board([])
        self.assertEqual(len(self.driver.find_elements("css selector", ".staged-leg")), 4)
        first.update(status="COMPLETED", set_tally=[3, 0], points=[11, 5], period="3rd")
        self.board([first, second])  # Incomplete set evidence must not clear staged bets.
        self.assertEqual(len(self.driver.find_elements("css selector", ".staged-leg")), 4)
        first["sets"] = [{"set": i, "home": 11, "away": 5} for i in (1, 2, 3)]
        first["score_at"] = (datetime.now(timezone.utc)-timedelta(seconds=20)).isoformat()
        self.board([first, second])
        self.assertEqual(len(self.driver.find_elements("css selector", ".staged-leg")), 4)
        first["score_at"] = datetime.now(timezone.utc).isoformat()
        self.board([first, second])
        self.assertEqual(len(self.driver.find_elements("css selector", ".staged-leg")), 2)
        self.assertEqual(self.card("101").find_elements("css selector", ".staged-leg"), [])
        self.emit(kind="slip", legs=[legs[2]], checked_at=datetime.now(timezone.utc).isoformat())
        self.assertEqual(len(self.driver.find_elements("css selector", ".staged-leg")), 2)
        self.assert_no_start()

    def test_two_positions_track_their_own_games_and_observed_decisions(self):
        first, second = board_event(), board_event("202", ["Cy East", "Dee West"], reverse=True)
        self.board([first, second])
        self.emit(kind="positions", records=[position(first, "p1"), position(second, "p2")])
        self.assertEqual((self.tracking("p1"), self.tracking("p2")), ("Ahead", "Behind"))
        self.assertNotIn("probability", self.value("board-events").lower())
        first.update(set_tally=[2, 0], points=[0, 0], period="3rd")
        first["sets"][1] = {"set": 2, "home": 11, "away": 8}
        second.update(set_tally=[0, 2], points=[0, 0], period="3rd")
        second["sets"][1] = {"set": 2, "home": 8, "away": 11}
        self.board([first, second])
        self.emit(kind="positions", records=[])
        self.assertEqual((self.tracking("p1"), self.tracking("p2")), ("Observed win", "Observed loss"))
        self.assertNotIn("settled", self.card("101").text.lower())
        self.board([first])
        self.assertEqual(self.tracking("p2"), "Unknown")
        self.assert_no_start()

    def test_positions_fail_closed_for_unverified_identity_target_and_submission(self):
        event = board_event()
        self.board([event])
        valid = position(event, "valid", specification={"family": "split", "player": 0, "target": 2})
        cases = {}
        for name in ("contract", "players", "target", "family", "selection", "submission", "conflict"):
            record = deepcopy(valid); record["id"] = name
            if name == "contract": record["contract"] = "202:split"
            if name == "players": record["snapshot"]["players"].reverse()
            if name == "target":
                record["snapshot"].pop("target_game"); record["snapshot"].pop("specification")
            if name == "family": record["snapshot"]["specification"]["family"] = "point_handicap"
            if name == "selection": record["snapshot"]["selection"] = event["players"][1]
            if name == "submission": record["outcome"] = "uncertain"
            if name == "conflict": record["snapshot"]["specification"]["target"] = 3
            cases[name] = record
        self.emit(kind="positions", records=[valid, *cases.values()])
        self.assertEqual(self.tracking("valid"), "Ahead")
        for name in cases:
            with self.subTest(name=name): self.assertEqual(self.tracking(name), "Unknown")
        event["points"] = None
        self.board([event])
        self.assertEqual(self.tracking("valid"), "Unknown")
        self.assert_no_start()

    def test_match_positions_only_decide_on_verified_completion_and_keep_entry_ev_historical(self):
        event = board_event()
        record = position(event, "match", bet_type="closeout", point_ev=40)
        self.board([event])
        self.emit(kind="positions", records=[record])
        self.assertEqual(self.tracking("match"), "Ahead")
        event.update(status="COMPLETED", set_tally=[3, 0])
        self.board([event])
        self.assertEqual(self.tracking("match"), "Unknown")
        event["sets"] = [{"set": i, "home": 11, "away": 6} for i in (1, 2, 3)]
        self.board([event])
        self.assertEqual(self.tracking("match"), "Observed win")
        self.assertIn("Entry point EV +40.00% · historical", self.card("101").text)
        self.assertNotIn("Conservative EV +12.50%", self.card("101").text)

    def test_ambiguous_ids_missing_points_and_unsupported_targets_never_get_live_values(self):
        event = board_event()
        self.board([event])
        self.board([event, deepcopy(event)])
        self.assertEqual(self.value("board-count"), "1")
        self.assertEqual(self.card("101").get_attribute("data-stale"), "true")
        self.assertNotIn("Conservative EV +12.50%", self.card("101").text)
        self.board([event])
        event["markets"][0]["outcomes"] *= 2
        self.board([event])
        self.assertEqual(len(self.card("101").find_elements("css selector", ".board-offer")), 1)
        self.assertNotIn("Conservative EV +12.50%", self.card("101").text)
        event = board_event()
        event["points"] = None
        self.board([event])
        self.assertNotIn("Conservative EV +12.50%", self.card("101").text)
        self.emit(kind="positions", records=[
            position(event, "total", bet_type="match_points", selection="Over", specification={"family": "match_points", "over": True, "line": 74.5}),
            position(event, "handicap", bet_type="point_handicap", specification={"family": "point_handicap", "player": 0, "line": 1.5}),
            position(event, "unsupported", bet_type="future_strategy"),
            position(event, "bad_player", specification={"family": "split", "player": 2, "target": 2})])
        for identifier in ("total", "handicap", "unsupported", "bad_player"):
            self.assertEqual(self.tracking(identifier), "Unknown")
        self.assert_no_start()

    def test_point_and_handicap_positions_use_observed_points_without_probabilities(self):
        event = board_event()
        self.board([event])
        self.emit(kind="positions", records=[
            position(event, "total", bet_type="match_points", selection="Over", specification={"family": "match_points", "over": True, "line": 74.5}),
            position(event, "points", bet_type="point_handicap", specification={"family": "point_handicap", "player": 0, "line": -7.5}),
            position(event, "sets", bet_type="set_handicap", specification={"family": "set_handicap", "player": 0, "line": 1.5})])
        # 11+7+6+4 = 28 observed total; point margin 4+2-7.5 = -1.5; set margin 1+1.5 = 2.5.
        self.assertEqual(self.tracking("total"), "Behind")
        self.assertEqual(self.tracking("points"), "Behind")
        self.assertEqual(self.tracking("sets"), "Ahead")
        self.assertIn("Observed total 28", self.card("101").text)
        self.assertNotIn("probability", self.card("101").text.lower())
        self.assert_no_start()

    def test_filters_are_keyboard_accessible_and_never_start_execution(self):
        first, second = board_event(), board_event("202", ["Cy East", "Dee West"])
        self.board([first, second])
        self.emit(kind="positions", records=[position(first, "p1")])
        self.emit(kind="slip", checked_at=datetime.now(timezone.utc).isoformat(), legs=[{
            "event_id": "202", "market_id": "m202", "selection_id": "s202", "label": "Cy East", "market": "Game 2"}])
        for name, expected in (("mine", ["101"]), ("staged", ["202"]), ("live", ["101", "202"]), ("all", ["101", "202"])):
            button = self.driver.find_element("css selector", f'[data-filter="{name}"]')
            button.send_keys(Keys.ENTER)
            self.assertEqual(button.get_attribute("aria-pressed"), "true")
            self.assertEqual([card.get_attribute("data-event-id") for card in self.driver.find_elements("css selector", ".match-card")], expected)
        self.assert_no_start()

    def test_optional_upcoming_shows_multiple_h2hs_without_inventing_live_ev(self):
        now = datetime.now(timezone.utc)
        self.emit(kind="scanner", connected=True, healthy=True, received_at=now.isoformat(), snapshot={"upcoming": {
            "protocol": 1, "updated_at": now.isoformat(), "expires_at": (now+timedelta(seconds=30)).isoformat(),
            "events": [{"key": str(i), "event_id": None, "players": names, "phase": "scheduled",
                        "scheduled_at": now.isoformat(), "history_count": 40+i} for i, names in enumerate((
                            ["Ada North", "Bea South"], ["Cy East", "Dee West"]))]}})
        self.assertEqual(self.value("board-count"), "2")
        self.assertIn("40 H2H meetings", self.value("board-events"))
        self.assertIn("41 H2H meetings", self.value("board-events"))
        self.assertNotIn("Conservative EV +", self.value("board-events"))
        self.assert_no_start()

    def test_board_untrusted_text_and_responsive_cards_at_phone_and_desktop_widths(self):
        evil = '<img src=x onerror="window.boardXss=true">'
        first, second = board_event(names=[evil, "Bea South"]), board_event("202", ["Cy East", "Dee West"])
        first["markets"][0]["name"] = evil
        first["markets"][0]["outcomes"][0]["label"] = evil
        self.board([first, second])
        self.emit(kind="slip", legs=[{"event_id": "101", "market_id": "m101", "selection_id": "s101", "label": evil, "market": evil}])
        self.emit(kind="positions", records=[position(first, "xss", selection=evil, market=evil)])
        self.assertIn(evil, self.card("101").text)
        self.assertEqual(self.driver.find_elements("css selector", "#board-events img, #board-events script"), [])
        self.assertFalse(self.driver.execute_script("return window.boardXss === true"))
        for width in (360, 580, 1040, 1440):
            with self.subTest(width=width):
                self.driver.set_window_size(width, 1000)
                self.assertTrue(self.driver.execute_script("return document.documentElement.scrollWidth <= innerWidth + 1"))
                self.assertTrue(self.card("101").is_displayed())
                self.assertTrue(self.card("202").is_displayed())
                self.assertTrue(self.driver.execute_script("return [...document.querySelectorAll('.board-offer')].every(n=>n.scrollWidth<=n.clientWidth+1)"))
        self.driver.save_screenshot(str(Path(self.artifacts.name)/"match-board.png"))
        self.assert_no_start()

    def test_legacy_snapshot_stays_visible_and_updater_controls_keep_their_commands(self):
        self.show()
        self.assertTrue(self.driver.find_element("id", "opportunity-diagnostics").get_attribute("open"))
        self.assertIn("legacy snapshot", self.value("board-status"))
        self.emit(kind="signed_in", enabled=True, supported=True)
        self.driver.find_element("css selector", ".settings>summary").click()
        self.assertIn("up to date", self.value("update"))
        self.assertFalse(self.driver.find_element("id", "install").is_enabled())
        self.driver.execute_script("window.dashboardUpdate={available:true,version:'9.9.9',notes:'Fixture update'}")
        self.driver.find_element("id", "check").click()
        self.assertIn("9.9.9", self.value("update"))
        self.driver.find_element("id", "install").click()
        self.assertTrue(self.driver.execute_script("return window.dashboardCalls.some(c=>c.command==='install_update')"))
        self.assert_no_start()


    def test_validated_worker_board_uses_selected_confidence_without_client_math(self):
        from ember_client import Client
        from ember_protocol import RunLease, validate_scanner
        from selenium.webdriver.support.ui import Select
        now = datetime.now(timezone.utc)
        event = board_event()
        outcome = event["markets"][0]["outcomes"][0]
        outcome.update(conservative_ev=999, entry_ev=999, lower_probabilities={"90": 22.5, "95": 7.25},
                       entry_evs={"90": 30.5, "95": 18.75})
        scanner = {"healthy": True, "counts": {}, "board": {"protocol": 1, "updated_at": now.isoformat(),
                   "expires_at": (now+timedelta(seconds=10)).isoformat(), "events": [event]}}
        self.assertIsNotNone(validate_scanner(scanner)["board"])
        client = Client.__new__(Client)
        client.__dict__.update(lease=RunLease(), stream_connected=False, last_feed=None,
                               conservative_level="very_conservative", device_test=None)
        level = Select(self.driver.find_element("id", "conservative-level"))
        level.select_by_value("very_conservative")
        with patch("ember_client.emit", side_effect=lambda kind, **payload: self.emit(kind=kind, **payload)) as emitted:
            Client.accept_lease(client, {"server_time": now.isoformat(), "access_until": (now+timedelta(hours=1)).isoformat(),
                "enabled": True, "client_supported": True, "feed_healthy": True, "lease_seconds": 5, "scanner": scanner})
        relay = next(call.kwargs for call in emitted.call_args_list if call.args[0] == "scanner")
        self.assertEqual(relay["snapshot"]["board"]["events"][0]["markets"][0]["outcomes"][0]["conservative_ev"], 7.25)
        self.assertIn("Conservative EV +7.25%", self.card("101").text)
        self.assertIn("Entry EV +18.75%", self.card("101").text)
        level.select_by_value("conservative")
        self.assertIn("Conservative EV +22.50%", self.card("101").text)
        self.assertIn("Entry EV +30.50%", self.card("101").text)
        self.assertIn("90% confidence", self.value("board-status"))
        level.select_by_value("very_conservative")
        outcome["lower_probabilities"]["95"] = None
        outcome["entry_evs"].pop("95")
        self.board([event])
        self.assertIn("Conservative EV —", self.card("101").text)
        self.assertNotIn("Entry EV", self.card("101").text)
        self.assertNotIn("999", self.card("101").text)
        self.assert_no_start()

    def test_reference_window_status_tracks_selected_confidence_after_points_start(self):
        from selenium.webdriver.support.ui import Select
        event=board_event();outcome=event['markets'][0]['outcomes'][0]
        outcome.update(lower_probabilities={'90':10,'95':5},reference_windows={
            '90':{'kind':'pregame_non_decreasing','key':'a'*64,'status':'cancelled'},
            '95':{'kind':'pregame_non_decreasing','key':'b'*64,'status':'open'}})
        level=Select(self.driver.find_element('id','conservative-level'))
        level.select_by_value('very_conservative');self.board([event])
        self.assertIn('Entry open · frozen pre-set conservative reference',self.card('101').text)
        level.select_by_value('conservative')
        self.assertIn('Expired · conservative EV decreased or contract ended',self.card('101').text)
        self.assert_no_start()

    def test_real_v2_journal_positions_and_physical_slip_absence_are_distinct(self):
        from ember_client import Client
        from ember_history import OpportunityJournal
        from ember_protocol import validate_signal
        first, second = board_event(), board_event("202", ["Cy East", "Dee West"], reverse=True)
        for event in (first, second): event["markets"][0]["market_id"] = "9"+event["event_id"]
        self.board([first, second])
        legs = [{"event_id": event["event_id"], "market_id": event["markets"][0]["market_id"], "selection_id": "s"+event["event_id"],
                 "market": "Game 2", "label": event["players"][0], "line": None, "status": "ready", "decimal_odds": 1.95}
                for event in (first, second)]
        self.emit(kind="slip", legs=legs, checked_at=datetime.now(timezone.utc).isoformat())
        self.assertEqual(len(self.driver.find_elements("css selector", '[data-slip-state="present"]')), 2)
        self.emit(kind="slip", legs=[legs[1]], checked_at=datetime.now(timezone.utc).isoformat())
        self.assertIn("No longer in slip · last observation", self.card("101").text)
        self.assertIn("1 staged legs", self.value("board-status"))
        self.driver.find_element("css selector", '[data-filter="staged"]').click()
        self.assertEqual([card.get_attribute("data-event-id") for card in self.driver.find_elements("css selector", ".match-card")], ["202"])
        self.driver.find_element("css selector", '[data-filter="all"]').click()
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            journal = OpportunityJournal(Path(directory)/"history.sqlite3")
            scope = ("https://example.invalid", "fixture-account")
            for number, event in enumerate((first, second), 1):
                identifier = event["event_id"]
                signal = {"protocol": 2, "id": number, "contract": identifier+":split",
                    "expires_at": (now+timedelta(seconds=5)).isoformat(), "payload": {
                        "event": {"eventId": identifier, "player1": event["players"][0], "player2": event["players"][1],
                                  "status": "STARTED", "period": "2nd", "setTally": event["set_tally"],
                                  "sets": [event["sets"][0], {"set": 2, "home": 0, "away": 0}],
                                  "currentSet": {"set": 2, "home": 0, "away": 0}},
                        "market": {"eventId": identifier, "marketId": "9"+identifier, "name": "Game 2", "outcomes": [
                            {"selectionId": "s"+identifier, "label": event["players"][0], "decimal": 1.95, "points": None},
                            {"selectionId": "other"+identifier, "label": event["players"][1], "decimal": 1.95, "points": None}]},
                        "plan": {"betType": "split", "selection": event["players"][0], "selectionId": "s"+identifier,
                            "scoreState": event["set_tally"], "nextSet": 2, "specification": {"family": "split", "player": 0, "target": 2},
                            "min_decimal": 1.85, "stake_cents": 261, "deadline": (now+timedelta(seconds=4)).isoformat(), "display": {}}}}
                validate_signal(signal)
                journal.observe(scope, signal)
                journal.transition(scope, signal, "placed", "placed")
            reports = journal.snapshot(scope, outcome="placed")["records"]
            client = SimpleNamespace(history=journal, history_scope=lambda: scope, history_filter="missed", history_offset=0,
                history_error=False, history_signature=None, history_call=lambda func, *args, **kwargs: func(*args, **kwargs))
            with patch("ember_client.emit", side_effect=lambda kind, **payload: self.emit(kind=kind, **payload)):
                Client.publish_history(client)
        by_event = {record["snapshot"]["event_id"]: record for record in reports}
        self.assertEqual(self.tracking(by_event["101"]["id"]), "Ahead")
        self.assertEqual(self.tracking(by_event["202"]["id"]), "Behind")
        for record in reports:
            self.assertEqual(record["snapshot"]["bet_type"], "split")
            self.assertEqual(record["snapshot"]["target_game"], 2)
            self.assertEqual(record["snapshot"]["specification"], {"family": "split", "player": 0, "target": 2})
        self.assertEqual(len(self.driver.find_elements("css selector", '[data-slip-state="submitted"]')), 2)
        self.assertIn("0 staged legs", self.value("board-status"))
        self.emit(kind="slip", legs=[], checked_at=datetime.now(timezone.utc).isoformat())
        self.assertEqual(len(self.driver.find_elements("css selector", ".staged-leg")), 2)
        self.assertNotIn("Staged ·", self.value("board-events"))
        self.assert_no_start()

    def test_stage_catalog_cannot_imply_physical_slip_or_restore_complement(self):
        from ember_protocol import validate_scanner
        now = datetime.now(timezone.utc)
        stage = {"protocol": 1, "event_id": "101", "market_id": "m101", "selection_id": "s101", "label": "Ada North",
                 "bet_type": "complement", "market_name": "Game 2", "line": None, "updated_at": now.isoformat(),
                 "expires_at": (now+timedelta(seconds=5)).isoformat(),
                 "event": {"eventId": "101", "status": "STARTED", "player1": "Ada North", "player2": "Bea South"}}
        scanner = validate_scanner({"healthy": True, "counts": {}, "stages": [stage]})
        self.assertEqual(scanner["stages"], [])
        stage["bet_type"] = "split"
        scanner = validate_scanner({"healthy": True, "counts": {}, "stages": [stage]})
        self.assertEqual(len(scanner["stages"]), 1)
        self.emit(kind="scanner", connected=True, healthy=True, received_at=now.isoformat(), snapshot=scanner)
        self.assertEqual(self.driver.find_elements("css selector", ".staged-leg"), [])
        self.assertIn("0 staged legs", self.value("board-status"))
        self.assert_no_start()

    def test_saved_api_sanitization_keeps_public_endpoint_without_tunnel_history(self):
        source = (Path(__file__).parent/"ui"/"app.js").read_text(encoding="utf-8")
        self.assertIn("const previousApis = new Set();", source)
        self.assertNotRegex(source, r"https://[^\s'\"]+\.trycloudflare\.com")
        self.driver.execute_script("localStorage.setItem('ember-api','https://temporary-fixture.trycloudflare.com')")
        self.driver.refresh()
        self.assertEqual(self.driver.find_element("id", "api").get_attribute("value"), "https://getembersignals.com")
        self.assertEqual(self.driver.execute_script("return localStorage.getItem('ember-api')"), "https://getembersignals.com")
        self.assert_no_start()


if __name__ == "__main__":
    unittest.main()
