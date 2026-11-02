"""Per-match tabs: a cross-match turn switches to a rendered page instead of re-routing one."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest

from selenium.webdriver.chrome.options import Options

from driver_lifecycle import create_owned_chrome
import ember_tabs


class FakeBrowser:
    def __init__(self, url):
        self.pages = {'h0': url}
        self.current_window_handle = 'h0'
        self.calls = []
        self.opened = 0
        browser = self

        class Switch:
            def window(self, handle):
                browser.calls.append(('switch', handle))
                browser.current_window_handle = handle

            def new_window(self, kind):
                browser.opened += 1
                handle = f'h{browser.opened}'
                browser.pages[handle] = 'about:blank'
                browser.calls.append(('new', handle))
                browser.current_window_handle = handle
        self.switch_to = Switch()

    @property
    def window_handles(self):
        return list(self.pages)

    @property
    def current_url(self):
        return self.pages[self.current_window_handle]

    def get(self, url):
        self.calls.append(('get', url))
        self.pages[self.current_window_handle] = url

    def close(self):
        self.calls.append(('close', self.current_window_handle))
        self.pages.pop(self.current_window_handle)

    def execute_cdp_cmd(self, command, params):
        self.calls.append((command, self.current_window_handle))


def page(identifier):
    return f'https://sportsbook.draftkings.com/event/a-vs-b/{identifier}'


class EventTabTests(unittest.TestCase):
    def test_the_default_routes_in_place_and_never_opens_a_tab_but_still_uses_an_existing_one(self):
        # Live 2026-10-28: a new DK tab hydrated 20-30 s with every command into it hanging.
        driver = FakeBrowser(page('100'))
        self.assertIsNone(ember_tabs.show(driver, {'eventId': '101'}, page('101')))
        self.assertEqual(driver.pages, {'h0': page('100')})
        self.assertFalse([call for call in driver.calls if call[0] in ('new', 'get', 'close')])
        driver.pages['h1'] = page('101')  # A tab adopted from an earlier run is still switched to.
        driver._ember_tabs['h1'] = {'event': '101', 'used': 0}
        self.assertEqual(ember_tabs.show(driver, {'eventId': '101'}, page('101')), 'switched')

    def test_a_league_page_routes_in_place_and_a_match_page_is_kept_for_its_match(self):
        driver = FakeBrowser('https://sportsbook.draftkings.com/leagues/tabletennis/tt-elite-series')
        self.assertIsNone(ember_tabs.show(driver, {'eventId': '100'}, page('100')))
        self.assertEqual(driver.calls, [])
        driver.pages['h0'] = page('100')
        self.assertEqual(ember_tabs.show(driver, {'eventId': '101'}, page('101'), limit=4), 'opened')
        self.assertEqual(driver.pages, {'h0': page('100'), 'h1': page('101')})
        self.assertIsNone(ember_tabs.show(driver, {'eventId': '101'}, page('101'), limit=4))
        driver.calls.clear()
        self.assertEqual(ember_tabs.show(driver, {'eventId': '100'}, page('100'), limit=4), 'switched')
        self.assertEqual(driver.calls, [('switch', 'h0'), ('Page.bringToFront', 'h0')])

    def test_the_least_recent_match_tab_closes_at_the_limit_and_a_rerouted_tab_is_relearned(self):
        clock = iter(range(100))
        driver = FakeBrowser(page('100'))
        for identifier in ('101', '102'):
            ember_tabs.show(driver, {'eventId': identifier}, page(identifier), limit=3, clock=lambda: next(clock))
        self.assertEqual(ember_tabs.show(driver, {'eventId': '103'}, page('103'), limit=3, clock=lambda: next(clock)), 'opened')
        self.assertNotIn(page('100'), driver.pages.values())
        self.assertEqual(len(driver.pages), 3)
        self.assertIn(('close', 'h0'), driver.calls)
        # Another component re-routed the 101 tab: the stale record cannot be used.
        driver.pages['h1'] = page('109')
        self.assertEqual(ember_tabs.show(driver, {'eventId': '101'}, page('101'), limit=9, clock=lambda: next(clock)), 'opened')
        self.assertEqual(driver.current_url, page('101'))
        self.assertEqual(driver._ember_tabs['h1']['event'], '109')

    def test_finished_match_tabs_close_after_a_grace_but_one_tab_always_stays(self):
        clock = [0]
        driver = FakeBrowser(page('100'))
        for identifier in ('101', '102'):
            ember_tabs.show(driver, {'eventId': identifier}, page(identifier), limit=4, clock=lambda: clock[0])
        self.assertEqual(driver.current_url, page('102'))
        self.assertEqual(ember_tabs.retire(driver, None, clock=lambda: clock[0]), [])  # Unknown board: never prune.
        self.assertEqual(ember_tabs.retire(driver, {'102'}, clock=lambda: clock[0]), [])  # A reconnect gap is not an end.
        clock[0] = 30
        self.assertEqual(ember_tabs.retire(driver, {'101', '102'}, clock=lambda: clock[0]), [])
        clock[0] = 61
        self.assertEqual(ember_tabs.retire(driver, {'102'}, clock=lambda: clock[0]), ['100'])  # Off the board since 0 s.
        clock[0] = 100
        self.assertEqual(ember_tabs.retire(driver, set(), clock=lambda: clock[0]), [])  # 101 came back at 30 s.
        clock[0] = 122
        self.assertEqual(ember_tabs.retire(driver, set(), clock=lambda: clock[0]), ['101'])
        self.assertEqual(list(driver.pages.values()), [page('102')])  # The current tab is never closed.
        self.assertEqual(driver.current_url, page('102'))
        # The finished current page is free to re-route in place instead of opening another tab.
        self.assertIsNone(ember_tabs.show(driver, {'eventId': '103'}, page('103'), limit=4, clock=lambda: clock[0]))
        self.assertEqual(len(driver.pages), 1)

    def test_a_reattached_driver_keeps_and_retires_tabs_a_lost_driver_opened(self):
        # Live 2026-10-27: every recovery attached a new driver with an empty record, so finished
        # match tabs were never closed and every tab was re-read each poll.
        clock = [0]
        first = FakeBrowser(page('100'))
        book = {}
        ember_tabs.adopt(first, book, clock=lambda: clock[0])
        ember_tabs.show(first, {'eventId': '101'}, page('101'), limit=4, clock=lambda: clock[0])
        first.pages['h9'] = 'https://sportsbook.draftkings.com/auth/login'  # A sign-in popup.
        second = FakeBrowser(page('100'))
        second.pages, second.current_window_handle = first.pages, 'h1'
        self.assertEqual(ember_tabs.adopt(second, {}, clock=lambda: clock[0]), ['h0', 'h1'])  # A fresh worker relearns them.
        self.assertEqual(second.current_window_handle, 'h1')
        self.assertNotIn('h9', second._ember_tabs)
        ember_tabs.adopt(second, book, clock=lambda: clock[0])  # Same worker: the record carries over.
        self.assertIs(second._ember_tabs, book)
        self.assertEqual(ember_tabs.retire(second, {'101'}, clock=lambda: clock[0]), [])
        clock[0] = 61
        self.assertEqual(ember_tabs.retire(second, {'101'}, clock=lambda: clock[0]), ['100'])
        self.assertEqual(sorted(second.pages), ['h1', 'h9'])

    def test_the_working_tab_is_the_one_last_used(self):
        self.assertEqual(ember_tabs.working({'h0': {'used': 3}, 'h1': {'used': 7}, 'h2': {}}), 'h1')
        for empty in ({}, None):
            self.assertIsNone(ember_tabs.working(empty))

    def test_a_stale_or_decided_board_row_no_longer_counts_as_live(self):
        now = 1_800_000_000
        stamp = lambda age: datetime.fromtimestamp(now-age, timezone.utc).isoformat()
        board = {'events': [
            {'event_id': '1', 'status': 'STARTED', 'set_tally': [1, 1], 'score_at': stamp(30)},
            {'event_id': '2', 'status': 'STARTED', 'set_tally': [1, 1], 'score_at': stamp(ember_tabs.LIVE_SCORE_AGE+1)},
            {'event_id': '3', 'status': 'STARTED', 'set_tally': [3, 1], 'score_at': stamp(5)},
            {'event_id': '4', 'status': 'FINISHED', 'set_tally': [2, 1], 'score_at': stamp(5)},
            {'event_id': '5', 'status': 'STARTED', 'set_tally': None, 'score_at': stamp(ember_tabs.LIVE_SCORE_AGE)}]}
        self.assertEqual(ember_tabs.live_events(board, now), {'1', '5'})

    def test_only_event_urls_identify_a_match(self):
        for url, expected in ((page('34906453'), '34906453'), (page('34906453')+'?category=odds', '34906453'),
                              ('https://sportsbook.draftkings.com/leagues/tabletennis', None), (None, None)):
            with self.subTest(url=url):
                self.assertEqual(ember_tabs.event_of(url), expected)

    def test_cross_match_navigation_switches_tabs_before_any_link_click_or_reload(self):
        from unittest.mock import Mock, patch
        import tt_market_exec as execution
        driver = Mock(current_url=page('100'), _tt_navigation_intent=None)
        event = {'eventId': '101', 'player1': 'One', 'player2': 'Two'}
        shown = []

        def switch(driver, event, url):
            shown.append(url)
            driver.current_url = page('101')
            return 'switched'
        with patch.object(execution, 'correct_event', side_effect=lambda d, e: d.current_url == page('101')), \
                patch.object(execution.event_tabs, 'show', side_effect=switch), \
                patch.object(execution, 'market_tab', return_value=Mock(text='Game Props', get_attribute=lambda name: 'true')):
            self.assertTrue(execution.open_props(driver, event, {'name': 'Game 2'}, timeout=.4))
        self.assertEqual(shown, ['https://sportsbook.draftkings.com/event/one-vs-two/101'])
        driver.get.assert_not_called()
        driver.find_elements.assert_not_called()
        self.assertEqual(driver._tt_last_navigation, 'tab switch')


class Pages(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (f'<!doctype html><title>{self.path}</title><div id=page>{self.path}</div>').encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class EventTabBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Pages)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)
        options = Options()
        options.add_argument('--headless=new')
        cls.driver = create_owned_chrome(options)
        cls.addClassCleanup(cls.driver.quit)

    def test_returning_to_a_match_reuses_its_rendered_page_without_reloading(self):
        driver = self.driver
        url = lambda identifier: f'http://127.0.0.1:{self.server.server_port}/event/a-vs-b/{identifier}'
        driver.get(url('100'))
        driver.execute_script('window.rendered="100 stays rendered"')
        self.assertEqual(ember_tabs.show(driver, {'eventId': '101'}, url('101'), limit=4), 'opened')
        self.assertTrue(driver.current_url.endswith('/101'))
        self.assertEqual(ember_tabs.show(driver, {'eventId': '100'}, url('100'), limit=4), 'switched')
        self.assertTrue(driver.current_url.endswith('/100'))
        self.assertEqual(driver.execute_script('return window.rendered'), '100 stays rendered')
        self.assertEqual(len(driver.window_handles), 2)


if __name__ == '__main__':
    unittest.main()
