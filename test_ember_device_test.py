from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest

from ember_device_test import DeviceTest


class DeviceTestControls(unittest.TestCase):
    def test_one_opportunity_cannot_be_rearmed_or_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            test = DeviceTest(Path(directory), "test", clock=lambda: 1)
            self.assertFalse(test.permits())
            test.begin(0)
            self.assertTrue(test.permits())
            signal = {"id": 10, "payload": {"event": {"eventId": "100"}}}
            self.assertTrue(test.claim(signal))
            self.assertFalse(test.claim(signal))
            self.assertFalse(test.permits({"id": 11}))
            test.finish("placed")
            self.assertFalse(test.permits(signal))
            with self.assertRaises(RuntimeError): test.begin(0)

    def test_policy_and_timeout_do_not_relax_model_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [0]
            test = DeviceTest(Path(directory), "test", clock=lambda: clock[0], timeout=20)
            test.begin(0)
            with self.assertRaises(RuntimeError): test.policy({"ev": 0})
            test.policy({"ev": 0, "ev_mode": "conservative", "confidence": 95, "cost": 0})
            clock[0] = 21
            self.assertFalse(test.permits())

    def test_reports_reject_credentials_and_keep_uncertain_submission_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            test = DeviceTest(Path(directory), "test")
            with self.assertRaises(ValueError): test.update(access_token="never persist credentials")
            test.update(submission_dispatched=True, submitted=None)
            test.execution({"status": "stopped", "sizing": {"stake": 2, "password": "not recorded"}})
            self.assertEqual(test.state["outcome"], "uncertain_submission")
            self.assertNotIn("password", test.path.read_text())

    def test_auth_probe_can_return_immediately_for_a_signed_out_browser(self):
        engine = Path(__file__).parent/"desktop/engine"
        code = (
            f"import sys,time;sys.path.insert(0,{str(engine)!r});"
            "from types import SimpleNamespace;from draftkings_login import has_saved_session;"
            "driver=SimpleNamespace(window_handles=['owned'],current_url='https://sportsbook.draftkings.com/',"
            "switch_to=SimpleNamespace(window=lambda _:None),execute_script=lambda *_:{'state':'signed-out'});"
            "started=time.monotonic();assert not has_saved_session(driver,timeout=0);assert time.monotonic()-started<1"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.name == "nt", "The extracted launcher is Windows-specific")
    def test_packaged_browser_launcher_does_not_require_a_development_venv(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = Path(directory)/"start_draftkings.ps1"
            source = Path(__file__).parent/"start_draftkings.ps1"
            launcher.write_text(source.read_text())
            command = (
                "function Test-Path { param($LiteralPath) return (-not ($LiteralPath -like '*.venv*')) }; "
                "function Get-NetTCPConnection { param($LocalPort,$State,$ErrorAction) return @() }; "
                "function Start-Process { param($FilePath,$ArgumentList,$WindowStyle) }; "
                "function Invoke-RestMethod { param($Uri,$TimeoutSec) return @{Browser='Chrome/test'} }; "
                f"& '{launcher}' -BrowserOnly -DeferNavigation -ProfileDirectory '{directory}\\profile' -Port 19347"
            )
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            self.assertIn("Private Chrome is ready", result.stdout)


if __name__ == "__main__":
    unittest.main()
