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

    def test_other_ember_match_tabs_are_not_reread_and_the_working_tab_stays_current(self):
        counter = "Object.defineProperty(window,'__isLoggedIn',{get(){window.reads=(window.reads||0)+1;return true},configurable:true})"
        self.wagering_ready()
        self.driver.execute_script(counter)
        other = self.driver.current_window_handle
        self.driver.switch_to.new_window("tab")
        self.driver.get(self.base+"/event/two/2")
        self.wagering_ready()
        self.driver.execute_script(counter)
        working = self.driver.current_window_handle
        self.driver._ember_tabs = {other: {"event": "1"}, working: {"event": "2"}}
        self.addCleanup(delattr, self.driver, "_ember_tabs")
        self.driver.switch_to.new_window("tab")  # A window Ember did not open, e.g. a DraftKings popup.
        self.driver.get(self.base+"/verify")
        self.driver.execute_script("document.getElementById('login').remove();document.getElementById('form').innerHTML=''")
        popup = self.driver.current_window_handle
        self.driver.switch_to.window(working)
        self.assertTrue(provider.provider_state(self.driver)["signed_in"])
        self.assertEqual(self.driver.current_window_handle, working)
        self.assertGreater(self.driver.execute_script("return window.reads||0"), 0)
        self.driver.switch_to.window(other)
        self.assertEqual(self.driver.execute_script("return window.reads||0"), 0)
        # The non-Ember window is still read, so its verification prompt keeps priority.
        self.driver.switch_to.window(popup)
        self.driver.execute_script("document.getElementById('form').innerHTML='<input autocomplete=one-time-code>'")
        self.driver.switch_to.window(working)
        self.assertTrue(provider.provider_state(self.driver)["verification_pending"])
        self.assertEqual(self.driver.current_window_handle, popup)

    def test_login_opener_rechecks_fields_before_returning_a_click_target(self):
        self.driver.execute_script("document.getElementById('form').innerHTML='<input id=login-username-input>'")
        result = self.driver.execute_script(provider.OPEN_LOGIN_SCRIPT, {"origin": self.base})
        self.assertEqual(result["status"], "already-open")
        self.assertEqual(self.driver.execute_script("return window.opens"), 0)

    def test_shared_ancestor_visibility_matches_the_per_element_walk_exactly(self):
        # The pre-2026-10-27 walk is the oracle; every hidden-ancestor kind, nesting and order.
        self.driver.execute_script("""document.body.insertAdjacentHTML('beforeend', `
            <div id=t><div style="display:none"><span>a</span><div><span>b</span></div></div>
            <div style="visibility:hidden"><span>c</span><span style="visibility:visible">d</span></div>
            <div style="opacity:0"><div><a href=#>e</a></div></div><div style="opacity:.01"><span>f</span></div>
            <span style="display:contents"><button>g</button></span><div style="width:0;height:0;overflow:hidden"><span>h</span></div>
            <div><div><div><span>i</span><span style="opacity:0">j</span></div></div></div><span></span></div>`)""")
        verdicts = self.driver.execute_script(provider.VISIBLE_JS + r"""
            const walk=e=>{if(!e?.getClientRects().length)return false;for(let p=e;p;p=p.parentElement){
                const s=getComputedStyle(p);if(s.display==='none'||s.visibility==='hidden'||Number(s.opacity)===0)return false;}return true;};
            const all=[...document.querySelectorAll('*')];
            return [all.map(walk), all.map(visible), [...all].reverse().map(visible).reverse()];""")
        self.assertEqual(verdicts[0], verdicts[1])
        self.assertEqual(verdicts[0], verdicts[2])
        self.assertIn(True, verdicts[0])
        self.assertIn(False, verdicts[0])

    def test_only_a_visible_login_control_overrides_the_native_signed_in_flag(self):
        self.wagering_ready()
        self.driver.execute_script("document.body.insertAdjacentHTML('beforeend','<div style=\"opacity:0\"><div><button>Log In</button></div></div>')")
        self.assertTrue(provider.provider_state(self.driver)["signed_in"])
        self.driver.execute_script("document.body.insertAdjacentHTML('beforeend','<div><div><button>Log In</button></div></div>')")
        state = provider.provider_state(self.driver)
        self.assertFalse(state["signed_in"])
        self.assertTrue(state["explicit_signed_out"])

    def test_text_prefiltered_login_detection_matches_the_full_element_walk(self):
        # The pre-2026-10-31 walk checked visibility of every element before its text; it is the oracle.
        oracle = provider.VISIBLE_JS + r"""
            const text=e=>(e.innerText||e.textContent||'').replace(/\s+/g,' ').trim();
            return [...document.querySelectorAll('button,a,[role="button"],span,div')].filter(visible)
                .some(e=>/^(?:sign\s*up\s*(?:or|\/)\s*)?(?:log\s*in|sign\s*in)$/i.test(text(e)));"""
        # Hidden cases carry visible sibling text: a visible box whose only text is hidden reads as that
        # text (innerText falls back to textContent), in both versions alike.
        cases = {'<button>Log In</button>': True, '<p>odds</p><div style="display:none"><button>Log In</button></div>': False,
                 '<p>odds</p><span style="opacity:0">Sign In</span>': False, '<a href=#><span>Log</span><span>In</span></a>': True,
                 '<button style="text-transform:uppercase">log in</button>': True, '<div>Sign Up or Log In</div>': True,
                 '<div><span>Please log in to continue</span></div>': False, '<button>Log&nbsp;In</button>': True,
                 '<p>odds</p><div style="visibility:hidden"><span style="visibility:visible">Log In</span></div>': False,
                 '<div style="display:none"><button>Log In</button></div>': None,
                 '<div style="width:0;height:0;overflow:hidden"><button>Log In</button></div>': None,
                 ''.join(f'<span>row {i} odds +{i}</span>' for i in range(3000)) + '<div hidden>Log In</div>': False}
        self.wagering_ready()
        for html, expected in cases.items():
            with self.subTest(html=html[:60]):
                self.driver.execute_script("document.getElementById('case')?.remove();"
                    "document.body.insertAdjacentHTML('beforeend','<div id=case>'+arguments[0]+'</div>')", html)
                walked = self.driver.execute_script(oracle)
                self.assertEqual(provider.provider_state(self.driver)["signed_in"], not walked)
                if expected is not None:
                    self.assertEqual(walked, expected)

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
