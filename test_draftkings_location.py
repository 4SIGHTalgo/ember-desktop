"""Official location rechecks, with no coordinates, credentials or wager retries."""
import io
import json
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer
import threading
import unittest
from unittest.mock import Mock, patch

from selenium.webdriver.chrome.options import Options

from draftkings_location import LocationRecheck, LOCATION_RECHECK_SCRIPT, location_status
from driver_lifecycle import create_owned_chrome, DriverConnectionLost
from test_ember_provider_login import Fixture
import ember_provider_login as provider


class LocationRecheckTests(unittest.TestCase):
    def test_warning_absence_cannot_resume_before_all_native_evidence_returns(self):
        check=LocationRecheck({'attempted':True},clock=lambda:3)
        check.hold_until=2
        gate=provider.ProviderLoginGate(clock=lambda:3,recheck=check)
        healthy={'signed_in':True,'provider_can_bet':True,'native_wagering_grant':True,'document_ready':True}
        driver=Mock()
        for missing in ('document_ready','provider_can_bet','native_wagering_grant'):
            with self.subTest(missing=missing),patch.object(provider,'provider_state',return_value={**healthy,missing:False}):
                self.assertFalse(gate.poll(driver,lambda _:{'state':'unknown','blocked':False})['ready'])
                self.assertTrue(check.report['attempted'])
        with patch.object(provider,'provider_state',return_value=healthy):
            self.assertTrue(gate.poll(driver,lambda _:{'state':'unknown','blocked':False})['ready'])
        self.assertFalse(check.report['attempted'])
        driver.execute_script.assert_not_called()

    def test_driver_recovery_keeps_the_reserved_location_attempt(self):
        driver=Mock();link=Mock()
        driver.execute_script.return_value={'status':'ready','link':link}
        link.click.side_effect=DriverConnectionLost('reply lost')
        check=LocationRecheck(clock=lambda:0)
        state={'signed_in':True,'provider_can_bet':True,'native_wagering_grant':True,'document_ready':True}
        blocked=lambda _:{'state':'blocked','blocked':True}
        with patch.object(provider,'provider_state',return_value=state),redirect_stdout(io.StringIO()):
            with self.assertRaises(DriverConnectionLost):
                provider.ProviderLoginGate(clock=lambda:0,recheck=check).poll(driver,blocked)
            self.assertFalse(provider.ProviderLoginGate(clock=lambda:3,recheck=check).poll(driver,blocked)['ready'])
        driver.execute_script.assert_called_once_with(LOCATION_RECHECK_SCRIPT)
        link.click.assert_called_once()

    def test_stop_before_click_and_lost_reply_never_repeat_the_auth_action(self):
        driver=Mock()
        link=Mock()
        driver.execute_script.return_value={'status':'ready','link':link}
        check=LocationRecheck(clock=lambda:0)
        self.assertFalse(check.start(driver,{'state':'blocked'},validate=lambda:False))
        driver.execute_script.assert_not_called()
        valid=Mock(side_effect=[True,False])
        self.assertFalse(check.start(driver,{'state':'blocked'},validate=valid))
        link.click.assert_not_called()
        self.assertTrue(check.report['attempted'])
        check=LocationRecheck(clock=lambda:0)
        link.click.side_effect=DriverConnectionLost('fixture-secret')
        with self.assertRaises(DriverConnectionLost), redirect_stdout(io.StringIO()):
            check.start(driver,{'state':'blocked'})
        self.assertFalse(check.start(driver,{'state':'blocked'}))
        link.click.assert_called_once()
        self.assertNotIn('fixture-secret',json.dumps(check.report))


class LocationRecheckBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Fixture)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        options=Options();options.add_argument('--headless=new');options.add_argument('--window-size=1280,900')
        cls.driver=create_owned_chrome(options)
        cls.base=f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit();cls.server.shutdown();cls.server.server_close();cls.thread.join()

    def setUp(self):
        self.driver.get(self.base+'/?code=fixture-secret')
        self.driver.execute_script("document.getElementById('login').remove();window.__isLoggedIn=true;window.__canBet=false;window.__playSpan={playSpan:{clientKey:'fixture-only',grantType:'PlaySportsbook'}};window.rechecks=0;window.trusted=false")
        self.now=[0.0]
        native=self.driver
        base=self.base
        class LocalDriver:
            def execute_script(self,script,*args):
                return native.execute_script(script,{'origin':base}) if script==LOCATION_RECHECK_SCRIPT else native.execute_script(script,*args)
        self.local=LocalDriver()
        self.check=LocationRecheck(clock=lambda:self.now[0])
        self.gate=provider.ProviderLoginGate(clock=lambda:self.now[0],recheck=self.check)
        for item in (patch.object(provider,'SPORTSBOOK_URL',self.base),patch.object(provider,'LOGIN_URL',self.base+'/login')):
            item.start();self.addCleanup(item.stop)

    def banner(self,message="Sorry, it doesn't look like you're at a location where you can place bets."):
        self.driver.execute_script("""document.querySelector('.user-status-banner__wrapper')?.remove();
            const box=document.createElement('div');box.className='user-status-banner__wrapper';
            const text=document.createElement('span');text.className='user-status-banner__content';text.textContent=arguments[0];
            const link=document.createElement('a');link.href='/auth';link.setAttribute('aria-label','Recheck');link.textContent='Recheck';
            link.onclick=event=>{event.preventDefault();window.rechecks++;window.trusted=event.isTrusted};
            box.append(text,link);document.body.appendChild(box);""",message)

    def poll(self):
        with patch.object(provider,'provider_state',side_effect=lambda _:provider_state_real(self.driver)),redirect_stdout(io.StringIO()):
            return self.gate.poll(self.local,location_status)

    def test_blue_failure_rechecks_once_then_waits_for_actual_native_permission(self):
        self.banner()
        self.assertEqual(location_status(self.local)['state'],'blocked')
        for _ in range(3):
            self.assertFalse(self.poll()['ready'])
        self.assertEqual(self.driver.execute_script('return [window.rechecks,window.submits,window.trusted]'),[1,0,True])
        self.driver.execute_script("document.querySelector('.user-status-banner__wrapper').remove()")
        self.now[0]=1
        self.driver.execute_script('window.__canBet=true')
        self.assertFalse(self.poll()['ready'])
        self.driver.execute_script('window.__canBet=false')
        self.now[0]=3
        self.assertFalse(self.poll()['ready'])
        self.assertTrue(self.check.report['attempted'])
        self.driver.execute_script('window.__canBet=true')
        result=self.poll()
        self.assertTrue(result['ready'])
        self.assertFalse(self.check.report['attempted'])
        self.assertEqual(result['location']['state'],'unknown')
        self.assertNotIn('fixture-secret',json.dumps(result))
        self.assertNotIn('fixture-only',json.dumps(result))
        self.banner('We are having trouble verifying your location, which is required to bet.')
        self.assertFalse(self.poll()['ready'])
        self.assertEqual(self.driver.execute_script('return [window.rechecks,window.submits]'),[2,0])

    def test_wrong_ambiguous_disabled_or_unrelated_controls_are_never_clicked(self):
        for change in ("link.href='https://other.invalid/auth'", "link.href='/place-bet'", "link.target='_blank'",
                       "link.setAttribute('aria-disabled','true')", "box.appendChild(link.cloneNode(true))",
                       "box.querySelector('span').textContent='Your next event is ready'", "box.style.display='none'"):
            with self.subTest(change=change):
                self.banner()
                self.driver.execute_script("const box=document.querySelector('.user-status-banner__wrapper'),link=box.querySelector('a');"+change)
                self.assertEqual(self.local.execute_script(LOCATION_RECHECK_SCRIPT)['status'],'unavailable')
                self.assertEqual(self.driver.execute_script('return [window.rechecks,window.submits]'),[0,0])


provider_state_real=provider.provider_state

if __name__=='__main__':
    unittest.main()
