"""Native event ACL and real packaged-WebView/worker checks before publication."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest
from urllib.request import ProxyHandler, build_opener

import psutil
import websocket

ROOT = Path(__file__).resolve().parent


class NativeCapabilityTests(unittest.TestCase):
    def test_main_window_has_only_the_required_core_capabilities(self):
        capability = json.loads((ROOT/'src-tauri/capabilities/default.json').read_text())
        self.assertEqual(capability['windows'], ['main'])
        self.assertEqual(set(capability['permissions']), {'core:default', 'core:window:allow-close'})
        self.assertNotIn('remote', capability)


@unittest.skipUnless(os.name == 'nt', 'WebView2 CDP is a Windows native integration check')
class NativeSmokeTests(unittest.TestCase):
    def test_packaged_ui_receives_real_worker_events_and_can_close(self):
        binary = Path(os.environ.get('EMBER_NATIVE_SMOKE_BINARY',
            ROOT/'src-tauri/target/x86_64-pc-windows-msvc/release/ember-desktop.exe')).resolve()
        self.assertTrue(binary.is_file(), 'Build the native application before this check')
        version = json.loads((ROOT/'src-tauri/tauri.conf.json').read_text())['version']
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        with tempfile.TemporaryDirectory(prefix='ember-native-smoke-', ignore_cleanup_errors=True) as folder:
            environment = dict(os.environ, LOCALAPPDATA=folder, WEBVIEW2_USER_DATA_FOLDER=str(Path(folder)/'webview'),
                WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=f'--remote-debugging-port={port}'+
                    (' --no-sandbox --disable-gpu' if os.environ.get('GITHUB_ACTIONS')=='true' else ''))
            for key in ('EMBER_DEVICE_TEST_ONCE', 'EMBER_DEVICE_TEST_LOGIN_ONLY', 'EMBER_DEVICE_TEST_SCAN_ONLY',
                        'EMBER_DEVICE_BROWSER_TICKET'):
                environment.pop(key, None)
            process = subprocess.Popen([str(binary)], cwd=binary.parent, env=environment)
            connection = None
            sequence = 0
            def evaluate(expression):
                nonlocal sequence
                sequence += 1
                connection.send(json.dumps({'id':sequence, 'method':'Runtime.evaluate', 'params':{
                    'expression':expression, 'awaitPromise':True, 'returnByValue':True}}))
                while True:
                    reply = json.loads(connection.recv())
                    if reply.get('id') == sequence:
                        self.assertNotIn('error', reply)
                        result = reply['result']
                        self.assertNotIn('exceptionDetails', result, 'Native UI command/event permission failed')
                        return result['result'].get('value')
            try:
                deadline = time.monotonic()+45
                opener = build_opener(ProxyHandler({}))
                probe = {'stage':'debug_endpoint'}
                while time.monotonic() < deadline:
                    self.assertIsNone(process.poll(), 'Native app exited before its WebView became ready')
                    try:
                        with opener.open(f'http://127.0.0.1:{port}/json/list', timeout=1) as response:
                            pages = json.load(response)
                        probe = {'stage':'page_target','types':[p.get('type') for p in pages]}
                        page = next(p for p in pages if p['type']=='page' and 'tauri.localhost' in p['url'])
                        probe = {'stage':'debug_connection'}
                        connection = websocket.create_connection(page['webSocketDebuggerUrl'], timeout=30, suppress_origin=True,
                            http_no_proxy=['127.0.0.1','localhost'])
                        if evaluate("typeof window.__TAURI__==='object' && !!document.getElementById('status')"):
                            break
                        connection.close()
                        connection = None
                    except (OSError, StopIteration, websocket.WebSocketException) as error:
                        probe['error_type'] = type(error).__name__
                    time.sleep(.2)
                children = [child.name() for child in psutil.Process(process.pid).children(recursive=True)]
                self.assertIsNotNone(connection, f'Native WebView did not expose its local UI: {probe}; children={children}')
                observed = evaluate("""(async()=>{
                    let received,timer;
                    const response=new Promise(resolve=>received=resolve);
                    const off=await window.__TAURI__.event.listen('worker',({payload})=>{
                        if(payload.kind==='missed_history')received(true);
                    });
                    try {
                        await window.__TAURI__.core.invoke('missed_history');
                        return await Promise.race([response,new Promise((_,reject)=>{
                            timer=setTimeout(()=>reject(new Error('worker event timeout')),15000);
                        })]);
                    } finally { clearTimeout(timer);off(); }
                })()""")
                self.assertTrue(observed, 'A real worker response must traverse the native event ACL')
                for _ in range(100):
                    ready = evaluate("window.__TAURI__.core.invoke('worker_status')")
                    if ready:
                        break
                    time.sleep(.1)
                self.assertTrue(ready)
                self.assertEqual(evaluate("document.getElementById('version').textContent"), 'SPORTS SIGNALS · '+version)
                self.assertTrue(evaluate("document.getElementById('start').disabled"))
                self.assertEqual(evaluate("document.getElementById('session-state').textContent"), 'Stopped')
                self.assertFalse(evaluate("document.getElementById('login').disabled"))
                self.assertFalse((Path(folder)/'Ember/browser-profiles').exists())
                evaluate("setTimeout(() => window.__TAURI__.window.getCurrentWindow().close(), 50); true")
                self.assertEqual(process.wait(timeout=15), 0)
            finally:
                if connection is not None:
                    connection.close()
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
