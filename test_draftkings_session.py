import unittest
from unittest.mock import Mock,patch

from selenium.webdriver.chrome.options import Options

import draftkings_session as session
from driver_lifecycle import DriverConnectionLost,create_owned_chrome
from draftkings_session import SessionMaintenance


class FatalMaintenanceTests(unittest.TestCase):
    def test_optional_network_observation_never_swallows_a_fatal_driver_failure(self):
        driver=Mock();driver.get_log.side_effect=DriverConnectionLost('synthetic reply lost')
        with self.assertRaises(DriverConnectionLost):
            SessionMaintenance(45).observe_network(driver,0)
        driver.execute_script.assert_not_called()


class SessionMaintenanceTests(unittest.TestCase):
    def test_a_click_that_never_returns_holds_the_next_click_off_and_a_returning_click_does_not(self):
        keeper=session.SessionMaintenance(300)
        element=Mock()
        driver=Mock()
        driver.execute_script.return_value={'state':'ready','nativeSessionPresent':True,'canBet':True,
                                            'viewingOdds':True,'element':element}
        self.assertTrue(keeper.activity(driver,0,'signed-in'))
        self.assertEqual(keeper.backoff_until,0)  # A returning click leaves no hold.
        element.click.side_effect=session.WebDriverException("read timeout")
        with self.assertRaises(session.WebDriverException):
            keeper.activity(driver,45,'signed-in')
        element.click.side_effect=None
        self.assertFalse(keeper.activity(driver,90,'signed-in'))  # Still inside the hang backoff.
        self.assertTrue(keeper.activity(driver,45+session.CLICK_HANG_BACKOFF,'signed-in'))

    def test_waiting_keeps_the_session_and_a_covered_clock_retries_without_the_hang_hold(self):
        keeper=session.SessionMaintenance(300)
        element=Mock()
        driver=Mock()
        driver.execute_script.return_value={'state':'ready','element':element}
        self.assertTrue(keeper.activity(driver,0,'signed-in',waiting=True))
        driver.execute_script.assert_called_with(session.SESSION_ACTIVITY_SCRIPT,{'waiting':True})
        element.click.side_effect=session.ElementClickInterceptedException('overlay')
        self.assertFalse(keeper.activity(driver,45,'signed-in',waiting=True))
        self.assertEqual(keeper.report['state'],'session_indicator_covered')
        element.click.side_effect=None
        self.assertTrue(keeper.activity(driver,90,'signed-in',waiting=True))  # Not held off 5 minutes.
        self.assertFalse(keeper.activity(driver,135,'signed-out',waiting=True))
        self.assertEqual(element.click.call_count,3)


class SessionActivityBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        options=Options();options.add_argument('--headless=new')
        cls.driver=create_owned_chrome(options)

    @classmethod
    def tearDownClass(cls): cls.driver.quit()

    def setUp(self):
        self.driver.get('about:blank')
        self.driver.execute_script("""
            document.body.innerHTML='<button id="standardized-header-account-menu">Account</button>'+
                '<span id="sb-session-time">10 minutes</span><button id="wager">Place Bet</button>';
            window.__playSpan={playSpan:{clientKey:'PRIVATE_TEST_KEY',grantType:'PlaySportsbook'}};
            window.__canBet=window.__viewOdds=true;window.trustedActivity=0;window.wagers=0;
            document.addEventListener('click',e=>{if(e.isTrusted)window.trustedActivity++});
            document.getElementById('wager').onclick=()=>window.wagers++;
        """)
        # Only the origin check is adapted for an isolated local browser fixture.
        self.script=session.SESSION_ACTIVITY_SCRIPT.replace("location.hostname!=='sportsbook.draftkings.com'",'false')

    def test_click_is_trusted_and_never_touches_a_wager_or_exports_a_session_key(self):
        keeper=session.SessionMaintenance()
        with patch.object(session,'SESSION_ACTIVITY_SCRIPT',self.script):
            self.assertTrue(keeper.activity(self.driver,0,'signed-in'))
        self.assertEqual(self.driver.execute_script('return window.trustedActivity'),1)
        self.assertEqual(self.driver.execute_script('return window.wagers'),0)
        self.assertNotIn('PRIVATE',repr(keeper.report))

    def test_restricted_session_and_interactive_indicator_are_not_clicked(self):
        self.driver.execute_script("window.__playSpan.playSpan.grantType='Restricted'")
        keeper=session.SessionMaintenance()
        with patch.object(session,'SESSION_ACTIVITY_SCRIPT',self.script):
            self.assertFalse(keeper.activity(self.driver,0,'signed-in'))
        self.assertEqual(keeper.report['state'],'restricted')
        self.driver.execute_script("window.__playSpan.playSpan.grantType='PlaySportsbook';"
                                   "document.getElementById('wager').appendChild(document.getElementById('sb-session-time'))")
        with patch.object(session,'SESSION_ACTIVITY_SCRIPT',self.script):
            self.assertFalse(session.SessionMaintenance().activity(self.driver,0,'signed-in'))
        self.assertEqual(self.driver.execute_script('return window.wagers'),0)

    def test_native_authenticated_layout_can_maintain_session_without_the_legacy_header(self):
        self.driver.execute_script("document.getElementById('standardized-header-account-menu').remove();window.__isLoggedIn=true")
        keeper=session.SessionMaintenance()
        with patch.object(session,'SESSION_ACTIVITY_SCRIPT',self.script):
            self.assertTrue(keeper.activity(self.driver,0,'signed-in'))
        self.assertEqual(self.driver.execute_script('return window.trustedActivity'),1)
        self.assertEqual(self.driver.execute_script('return window.wagers'),0)

    def test_a_paused_signed_in_session_is_kept_alive_but_never_signed_out_or_through_an_overlay(self):
        self.driver.execute_script("window.__playSpan.playSpan.grantType='Restricted';window.__canBet=false;window.__isLoggedIn=true")
        with patch.object(session,'SESSION_ACTIVITY_SCRIPT',self.script):
            self.assertFalse(session.SessionMaintenance().activity(self.driver,0,'signed-in'))  # Betting mode still refuses.
            self.assertTrue(session.SessionMaintenance().activity(self.driver,0,'signed-in',waiting=True))
            self.driver.execute_script("window.__isLoggedIn=false")
            self.assertFalse(session.SessionMaintenance().activity(self.driver,0,'signed-in',waiting=True))
            self.driver.execute_script("""window.__isLoggedIn=true;document.body.insertAdjacentHTML('beforeend',
                '<div style="position:fixed;inset:0;z-index:9;background:#fff" onclick="window.overlay=(window.overlay||0)+1">Verify</div>')""")
            keeper=session.SessionMaintenance()
            self.assertFalse(keeper.activity(self.driver,0,'signed-in',waiting=True))
        self.assertEqual(keeper.report['state'],'session_indicator_covered')
        self.assertEqual(self.driver.execute_script('return [window.trustedActivity,window.wagers,window.overlay||0]'),[1,0,0])


if __name__=='__main__':
    unittest.main()
