"""Native event ACL and real packaged-WebView/worker checks before publication."""
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

import psutil

ROOT = Path(__file__).resolve().parent


class NativeCapabilityTests(unittest.TestCase):
    def test_main_window_has_only_the_required_core_capabilities(self):
        capability = json.loads((ROOT/'src-tauri/capabilities/default.json').read_text())
        self.assertEqual(capability['windows'], ['main'])
        self.assertEqual(set(capability['permissions']), {'core:default', 'core:window:allow-close'})
        self.assertNotIn('remote', capability)


def native_ui(pid, version, close=False):
    script = r'''
Add-Type -AssemblyName UIAutomationClient
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$pidCondition = [System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::ProcessIdProperty, PID_VALUE)
$windows = [System.Windows.Automation.AutomationElement]::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children, $pidCondition)
$report = @{window_found=$false; worker_version_observed=$false; start_disabled=$false; stopped=$false; login_enabled=$false}
foreach ($window in $windows) {
    if ($window.Current.Name -ne 'Ember') { continue }
    $report.window_found = $true
    $nodes = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($node in $nodes) {
        $name = $node.Current.Name
        if ($name -eq ('SPORTS SIGNALS '+[char]0xB7+' VERSION_VALUE')) { $report.worker_version_observed = $true }
        if ($name -eq 'Stopped') { $report.stopped = $true }
        if ($node.Current.ControlType -eq [System.Windows.Automation.ControlType]::Button) {
            if ($name -eq 'Start') { $report.start_disabled = !$node.Current.IsEnabled }
            if ($name -eq 'Sign in through browser') { $report.login_enabled = $node.Current.IsEnabled }
        }
    }
    CLOSE_ACTION
}
$report | ConvertTo-Json -Compress
'''.replace('PID_VALUE', str(pid)).replace('VERSION_VALUE', version).replace('CLOSE_ACTION',
        '$window.GetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern).Close()' if close else '')
    encoded = base64.b64encode(script.encode('utf-16le')).decode()
    result = subprocess.run(['powershell','-NoProfile','-NonInteractive','-EncodedCommand',encoded],
        capture_output=True, encoding='utf-8', timeout=15, check=True)
    return json.loads(result.stdout)


@unittest.skipUnless(os.name == 'nt', 'Windows UI Automation observes the real native WebView')
class NativeSmokeTests(unittest.TestCase):
    def test_packaged_ui_receives_real_worker_events_and_can_close(self):
        binary = Path(os.environ.get('EMBER_NATIVE_SMOKE_BINARY',
            ROOT/'src-tauri/target/x86_64-pc-windows-msvc/release/ember-desktop.exe')).resolve()
        self.assertTrue(binary.is_file(), 'Build the native application before this check')
        version = json.loads((ROOT/'src-tauri/tauri.conf.json').read_text())['version']
        with tempfile.TemporaryDirectory(prefix='ember-native-smoke-', ignore_cleanup_errors=True) as folder:
            environment = dict(os.environ, LOCALAPPDATA=folder, WEBVIEW2_USER_DATA_FOLDER=str(Path(folder)/'webview'),
                WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS='--force-renderer-accessibility')
            for key in ('EMBER_DEVICE_TEST_ONCE', 'EMBER_DEVICE_TEST_LOGIN_ONLY', 'EMBER_DEVICE_TEST_SCAN_ONLY',
                        'EMBER_DEVICE_BROWSER_TICKET'):
                environment.pop(key, None)
            process = subprocess.Popen([str(binary)], cwd=binary.parent, env=environment)
            try:
                deadline = time.monotonic()+45
                observed = {}
                while time.monotonic() < deadline:
                    self.assertIsNone(process.poll(), 'Native app exited before its WebView became ready')
                    observed = native_ui(process.pid, version)
                    if all(observed.values()):
                        break
                    time.sleep(.2)
                # This header is set only by the real worker's ready event, never HTML defaults.
                self.assertTrue(observed and all(observed.values()), observed)
                self.assertFalse((Path(folder)/'Ember/browser-profiles').exists())
                native_ui(process.pid, version, close=True)
                self.assertEqual(process.wait(timeout=15), 0)
            finally:
                if process.poll() is None:
                    owned = psutil.Process(process.pid)
                    children = owned.children(recursive=True)
                    owned.terminate()
                    for child in children:
                        try:
                            child.terminate()
                        except psutil.NoSuchProcess:
                            pass
                    process.wait(timeout=15)


if __name__ == '__main__':
    unittest.main()
