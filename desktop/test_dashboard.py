"""Real-browser dashboard fixtures. No provider site, account, or wager actions."""
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import unittest

from selenium.webdriver.chrome.options import Options

from driver_lifecycle import create_owned_chrome
from test_ember_opportunity import make_snapshot


BRIDGE = """
window.dashboardCalls=[];
window.__TAURI__={core:{invoke:async(command,args)=>{
    window.dashboardCalls.push({command,args});
    if(command==='test_alert_sound')return true;
    if(command==='check_update')return {available:false};
    return false;
}},event:{listen:async(name,callback)=>{window.dashboardWorker=callback;}}};
"""


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


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

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.driver.set_window_size(1040, 1000)
        self.driver.get(f"http://127.0.0.1:{self.server.server_port}/")

    def emit(self, **payload):
        self.driver.execute_script("window.dashboardWorker({payload:arguments[0]})", payload)

    def value(self, identifier):
        return self.driver.find_element("id", identifier).text

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
        self.assertIn("+60.00%", self.driver.find_element("id", "candidate-cards").text)
        self.assertIn("32 successes / 40 matching cases", self.driver.find_element("id", "candidate-cards").text)
        self.assertIn("2.00", self.value("market-name"))
        self.assertEqual(self.value("opportunity-stage"), "Qualified · execution stopped")

    def test_below_threshold_opportunity_is_visible_and_not_marked_ready(self):
        self.show(decimal=1.2, qualified=False)
        self.assertEqual(self.value("opportunity-stage"), "Below EV gate")
        self.assertIn("does not strictly exceed", self.value("decision-reason"))
        self.assertTrue(self.value("ev-value").startswith("−"))
        self.assertEqual(self.driver.find_element("id", "check-ev").get_attribute("data-passed"), "false")

    def test_frozen_history_warmup_shows_known_meetings_with_a_waiting_state(self):
        from test_ember_opportunity import OpportunityDisplayTests
        view, _, _, _ = OpportunityDisplayTests().warmup()
        now = datetime.now(timezone.utc)
        view.update(updated_at=now.isoformat(), expires_at=(now+timedelta(seconds=5)).isoformat(),
                    score_at=now.isoformat(), market_at=now.isoformat())
        self.emit(kind="scanner", connected=True, healthy=True, server_offset=0, received_at=now.isoformat(),
                  snapshot={"opportunity": view})
        self.assertIn("30 prior head-to-head meetings", self.value("opportunity-subtitle"))
        self.assertNotRegex(self.value("opportunity-subtitle"), r"\b0 prior head-to-head meetings")
        self.assertEqual(self.value("opportunity-stage"), "Waiting for data")
        self.assertIn("History fit warming up", self.value("decision-reason"))
        self.assertIn("0 eligible", self.value("decision-reason"))
        self.assertEqual(self.value("ev-value"), "—")
        self.assertEqual(self.driver.find_elements("css selector", "#candidate-cards .candidate"), [])

    def test_production_policy_displays_conservative_ev(self):
        import tt_elite_follow as core
        from ember_opportunity import opportunity_snapshot
        from test_ember_signal_service import policy_fixture, strategy_bettor, strategy_scan, NOW
        event, history, _, follower = policy_fixture(decimal=1.9)
        _, reports, _ = strategy_scan(core, follower, [history], strategy_bettor(core), NOW, True)
        now = datetime.now(timezone.utc)
        view = opportunity_snapshot(reports, follower.events, event["eventId"], [event["eventId"]], now)
        view.update(score_at=now.isoformat(), market_at=now.isoformat())
        self.emit(kind="scanner", connected=True, healthy=True, received_at=now.isoformat(),
                  snapshot={"opportunity": view})
        self.assertEqual(self.value("ev-label"), "CONSERVATIVE NET EV")
        self.assertEqual(self.value("ev-value"), f"+{view['candidates'][0]['gate_ev']:.2f}%")
        self.assertIn("Conservative EV > 0%", self.value("check-ev"))
        self.assertEqual(self.driver.find_element("id", "check-ev").get_attribute("data-passed"), "true")

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
        output = Path(__file__).parent/"build"
        output.mkdir(parents=True, exist_ok=True)
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
        output = Path(__file__).parent/"build"
        output.mkdir(parents=True, exist_ok=True)
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


if __name__ == "__main__":
    unittest.main()
