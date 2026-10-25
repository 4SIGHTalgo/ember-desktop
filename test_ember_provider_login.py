"""Provider login gate against real, credential-free local browser fixtures."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import patch

from selenium.webdriver.chrome.options import Options

from driver_lifecycle import create_owned_chrome
import draftkings_login
import ember_provider_login as provider


class BrowserClosureTests(unittest.TestCase):
    def test_no_remaining_windows_is_reported_as_browser_closure(self):
        from unittest.mock import Mock
        from selenium.common.exceptions import NoSuchWindowException
        with self.assertRaises(NoSuchWindowException):
            provider.provider_state(Mock(window_handles=[]))

    def test_a_navigation_timeout_is_not_mislabeled_as_a_closed_browser(self):
        from selenium.common.exceptions import TimeoutException, WebDriverException
        self.assertFalse(provider.browser_connection_lost(TimeoutException("page still loading")))
        self.assertTrue(provider.browser_connection_lost(WebDriverException("chrome not reachable")))


HTML = b"""<!doctype html><html><body>
<button id="standardized-header-account-menu">Account</button>
<div id="login"><span>Sign Up or Log In</span></div><div id="form"></div>
<script>window.__isLoggedIn=false;window.__canBet=false;window.opens=0;window.submits=0;
document.getElementById('login').onclick=()=>{window.opens++;
 document.getElementById('form').innerHTML='<input id="login-username-input"><input type="password" value="fixture-secret"><button onclick="window.submits++">Log In</button>';};
</script></body></html>"""


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(HTML)))
        self.end_headers()
        self.wfile.write(HTML)

    def log_message(self, *_):
        pass


class ProviderLoginBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1280,900")
        cls.driver = create_owned_chrome(options)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        for handle in self.driver.window_handles[1:]:
            self.driver.switch_to.window(handle)
            self.driver.close()
        self.driver.switch_to.window(self.driver.window_handles[0])
        self.driver.get(self.base+"/")
        for setting in (patch.object(provider, "SPORTSBOOK_URL", self.base+"/"),
                        patch.object(provider, "LOGIN_URL", self.base+"/login")):
            setting.start()
            self.addCleanup(setting.stop)
        self.clock = [0]
        self.gate = provider.ProviderLoginGate(clock=lambda: self.clock[0])
        self.location = lambda _: {"state": "unknown", "blocked": False}

    def signed_in(self):
        self.driver.execute_script("document.getElementById('login')?.remove();document.getElementById('form').innerHTML='';window.__isLoggedIn=true;")

    def wagering_ready(self):
        self.signed_in()
        self.driver.execute_script("window.__canBet=true;window.__playSpan={playSpan:{clientKey:'fixture-only',grantType:'PlaySportsbook'}}")

    def test_account_menu_alone_cannot_enable_scanning_and_login_opens_once(self):
        first = self.gate.poll(self.driver, self.location)
        self.assertFalse(first["signed_in"])
        self.assertFalse(first["ready"])
        self.clock[0] = 6
        self.gate.poll(self.driver, self.location)
        for _ in range(3):
            result = self.gate.poll(self.driver, self.location)
            self.assertEqual(result["phase"], "awaiting_signin")
        self.assertEqual(self.driver.execute_script("return [window.opens,window.submits]"), [1, 0])
        self.assertNotIn("fixture-secret", json.dumps(provider.provider_state(self.driver)))

    def test_logged_in_user_waits_for_native_wagering_permission_and_location(self):
        self.signed_in()
        self.assertEqual(self.gate.poll(self.driver, self.location)["phase"], "awaiting_location")
        self.driver.execute_script("window.__canBet=true;window.__playSpan={playSpan:{clientKey:'fixture-only',grantType:'Login'}}")
        self.assertFalse(self.gate.poll(self.driver, self.location)["ready"])
        self.driver.execute_script("window.__playSpan.playSpan.grantType='PlaySportsbook'")
        self.assertTrue(self.gate.poll(self.driver, self.location)["ready"])
        self.assertFalse(self.gate.poll(self.driver, lambda _: {"state": "blocked", "blocked": True})["ready"])

    def test_sms_challenge_blocks_even_stale_positive_native_flags(self):
        self.wagering_ready()
        self.driver.execute_script("document.body.insertAdjacentHTML('beforeend','<input autocomplete=one-time-code>')")
        result = self.gate.poll(self.driver, self.location)
        self.assertEqual(result["phase"], "awaiting_verification")
        self.assertFalse(result["ready"])
        self.assertEqual(self.driver.execute_script("return window.submits"), 0)

    def test_navigation_grace_prevents_reopening_login_during_page_hydration(self):
        self.wagering_ready()
        self.assertTrue(self.gate.poll(self.driver, self.location)["ready"])
        self.gate.navigation_started()
        self.driver.get(self.base+"/sports/tabletennis")
        self.clock[0] = 1
        self.assertFalse(self.gate.poll(self.driver, self.location)["ready"])
        self.assertEqual(self.driver.execute_script("return window.opens"), 0)
        self.wagering_ready()
        self.assertTrue(self.gate.poll(self.driver, self.location)["ready"])

    def test_pending_verification_popup_takes_priority_over_an_old_authenticated_tab(self):
        self.wagering_ready()
        original = self.driver.current_window_handle
        self.driver.switch_to.new_window("tab")
        self.driver.get(self.base+"/verify")
        verification = self.driver.current_window_handle
        self.driver.execute_script("document.getElementById('login').remove();document.getElementById('form').innerHTML='<input autocomplete=one-time-code>'")
        self.driver.switch_to.window(original)
        result = provider.provider_state(self.driver)
        self.assertTrue(result["verification_pending"])
        self.assertFalse(result["signed_in"])
        self.assertEqual(self.driver.current_window_handle, verification)

    def test_login_opener_rechecks_fields_before_returning_a_click_target(self):
        self.driver.execute_script("document.getElementById('form').innerHTML='<input id=login-username-input>'")
        result = self.driver.execute_script(provider.OPEN_LOGIN_SCRIPT, {"origin": self.base})
        self.assertEqual(result["status"], "already-open")
        self.assertEqual(self.driver.execute_script("return window.opens"), 0)

    def test_standby_navigation_confirms_the_tt_elite_league_route(self):
        original_get = self.driver.get
        def league_page(url):
            original_get(url)
            self.driver.execute_script("document.body.innerHTML='<h1>TT Elite Series Odds</h1>'")
        with patch.object(draftkings_login, "TT_ELITE_URL", self.base+"/leagues/tabletennis/tt-elite-series"), \
                patch.object(self.driver, "get", side_effect=league_page):
            draftkings_login.goto_tt_elite(self.driver)
        self.assertEqual(self.driver.current_url, self.base+"/leagues/tabletennis/tt-elite-series")


if __name__ == "__main__":
    unittest.main()
