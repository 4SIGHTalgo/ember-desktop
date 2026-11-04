"""A page command is never sent into a navigation that has not committed."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import unittest

from selenium.common.exceptions import NoSuchWindowException, WebDriverException
from selenium.webdriver.chrome.options import Options

from driver_lifecycle import connection_lost, create_owned_chrome
from ember_navigation import NavigationGate, NavigationPending


class FakeGuard:
    """Executes through the gate like _GuardedExecutor; history entries change only at 'commit'."""
    def __init__(self):
        self.handle, self.entries, self.sent = 'h0', {'h0': (1, 'https://a/1')}, []
        self.settled = []
        self.history_error = None  # a raw 4xx body, as the executor returns driver errors
        self.gate = NavigationGate(self, clock=lambda: self.now, sleep=self.tick, settled=lambda *row: self.settled.append(row))
        self.now = 0.0

    def tick(self, seconds):
        self.now += seconds

    def execute(self, command, params):
        self.gate.before(command, params)
        self.sent.append(command if command != 'executeCdpCommand' else params['cmd'])
        if command == 'executeCdpCommand' and self.history_error:
            response = {'status': 404, 'value': self.history_error}
        elif command == 'executeCdpCommand':
            entry, url = self.entries[self.handle]
            response = {'value': {'currentIndex': 0, 'entries': [{'id': entry, 'url': url}]}}
        elif command == 'w3cGetCurrentWindowHandle':
            response = {'value': self.handle}
        elif command == 'switchToWindow':
            self.handle = params['handle']
            response = {'value': None}
        else:
            response = {'value': 'ok'}
        self.gate.after(command, params, response)
        return response


class NavigationGateTests(unittest.TestCase):
    def test_page_commands_wait_briefly_then_are_refused_unsent_until_the_commit(self):
        guard = FakeGuard()
        guard.execute('get', {'url': 'https://a/2'})
        guard.sent.clear()
        with self.assertRaises(NavigationPending):
            guard.execute('w3cExecuteScript', {'script': 'return 1', 'args': []})
        self.assertNotIn('w3cExecuteScript', guard.sent)
        self.assertGreaterEqual(guard.now, NavigationGate.WAIT)
        for browser_side in (('w3cGetWindowHandles', {}), ('executeCdpCommand', {'cmd': 'Page.bringToFront', 'params': {}})):
            guard.execute(*browser_side)  # Answered by the browser process; never refused.
        guard.entries['h0'] = (2, 'https://a/2')  # The new document committed.
        self.assertEqual(guard.execute('getCurrentUrl', {}), {'value': 'ok'})
        self.assertEqual(guard.gate.pending, {})
        self.assertEqual(len(guard.settled), 1)
        self.assertEqual(guard.settled[0][1], 1)  # One refused, unsent command before the commit.

    def test_another_tab_is_usable_while_one_loads_and_a_reload_is_never_tracked(self):
        guard = FakeGuard()
        guard.entries['h1'] = (5, 'https://a/5')
        guard.execute('get', {'url': 'https://a/2'})
        guard.execute('switchToWindow', {'handle': 'h1'})
        self.assertEqual(guard.execute('w3cExecuteScript', {'script': 'return 1', 'args': []}), {'value': 'ok'})
        guard.execute('get', {'url': 'https://a/5'})  # Same URL: its entry id never changes, so do not gate it.
        self.assertEqual(guard.execute('findElements', {}), {'value': 'ok'})
        guard.execute('switchToWindow', {'handle': 'h0'})
        with self.assertRaises(NavigationPending):
            guard.execute('findElements', {})

    def test_a_navigation_that_never_commits_is_abandoned_to_normal_handling(self):
        guard = FakeGuard()
        guard.execute('get', {'url': 'https://a/2'})
        guard.now = NavigationGate.ABANDON+1
        self.assertEqual(guard.execute('getCurrentUrl', {}), {'value': 'ok'})

    def test_an_unreadable_history_raises_the_drivers_own_error_and_sends_nothing(self):
        # Full suite under load, 2026-10-27: a driver error body reached the gate as a string (TypeError).
        guard = FakeGuard()
        guard.execute('get', {'url': 'https://a/2'})
        guard.sent.clear()
        guard.history_error = json.dumps({'value': {'error': 'no such window', 'message': 'target window already closed'}})
        with self.assertRaises(NoSuchWindowException) as raised:
            guard.execute('w3cExecuteScript', {'script': 'return 1', 'args': []})
        self.assertTrue(connection_lost(raised.exception))  # Recovery still sees a lost window as one.
        self.assertEqual(guard.sent, ['Page.getNavigationHistory'])
        guard.sent.clear()
        guard.history_error = json.dumps({'value': {'error': 'timeout', 'message': 'Timed out receiving message from renderer'}})
        with self.assertRaises(NavigationPending):  # A busy renderer is "not ready yet", never a browser fault.
            guard.execute('w3cExecuteScript', {'script': 'return 1', 'args': []})
        self.assertEqual(guard.sent, ['Page.getNavigationHistory'])
        for shape in ({'currentIndex': 3, 'entries': []}, {'entries': [{}]}, 'text'):
            with self.subTest(shape=shape):
                guard.history_error = None
                guard.execute = lambda command, params, shape=shape: {'value': shape}
                with self.assertRaises(WebDriverException) as raised:
                    guard.gate.before('w3cExecuteScript', {'script': 'return 1', 'args': []})
                self.assertNotIsInstance(raised.exception, NavigationPending)


class SlowDocument(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/slow'):
            time.sleep(4)
        body = f'<!doctype html><title>{self.path}</title>{self.path}'.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class NavigationGateBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), SlowDocument)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)
        options = Options()
        options.add_argument('--headless=new')
        options.page_load_strategy = 'none'
        cls.driver = create_owned_chrome(options)
        cls.addClassCleanup(cls.driver.quit)
        cls.driver.command_guard.gate = NavigationGate(cls.driver.command_guard)

    def committed(self, driver, path, seconds=10):
        """Wait until the tab's document is the one at path; the gate refuses commands until it commits."""
        deadline = time.monotonic()+seconds
        while True:
            try:
                if driver.execute_script('return location.pathname') == path:
                    return
            except NavigationPending:
                pass
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.05)

    def test_a_slow_document_refuses_page_commands_quickly_while_another_tab_keeps_working(self):
        driver, base = self.driver, f'http://127.0.0.1:{self.server.server_port}'
        driver.get(base+'/fast')
        self.committed(driver, '/fast')
        first = driver.current_window_handle
        driver.switch_to.new_window('tab')
        second = driver.current_window_handle  # window_handles order differs across Chrome builds
        driver.get(base+'/fast2')
        self.committed(driver, '/fast2')  # A new tab starts a renderer: on a slow runner that took over 1 s.
        driver.switch_to.window(first)
        driver.get(base+'/slow')
        started = time.monotonic()
        with self.assertRaises(NavigationPending):
            driver.execute_script('return document.title')
        self.assertLess(time.monotonic()-started, 2)  # Without the gate this command is held ~4 s.
        driver.switch_to.window(second)
        self.assertEqual(driver.execute_script('return document.title'), '/fast2')
        driver.switch_to.window(first)
        self.committed(driver, '/slow')


if __name__ == '__main__':
    unittest.main()
