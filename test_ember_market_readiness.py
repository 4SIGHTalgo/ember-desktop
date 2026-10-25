"""Deadline recovery fixtures; no provider session, odds selection, or wager calls."""
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from selenium.common.exceptions import InvalidSessionIdException, WebDriverException
from selenium.webdriver.chrome.options import Options

from driver_lifecycle import create_owned_chrome
from ember_market_readiness import MarketReadiness
import tt_market_exec as execution


EVENT_ID = "34875708"
PLAYERS = ["Oliver Vincenec", "Lukasz Oracz"]
SELECTIONS = ["0QA381634356#2331449406_13L208037Q1-1582536807Q20",
              "0QA381634356#2331449413_13L208037Q1-1530293947Q20"]
EVENT = {"eventId": EVENT_ID, "player1": PLAYERS[0], "player2": PLAYERS[1], "status": "STARTED",
    "period": "1st", "setTally": [0, 1], "currentSet": {"set": 1, "home": 7, "away": 11},
    "sets": [{"set": 1, "home": 7, "away": 11}, {"set": 2, "home": 0, "away": 0}]}
MARKET = {"name": "Game 2", "betType": "split", "marketId": "381634356", "eventId": EVENT_ID,
    "outcomes": [{"selectionId": SELECTIONS[0], "label": PLAYERS[0], "american": "-124", "decimal": 1.8064516129},
                 {"selectionId": SELECTIONS[1], "label": PLAYERS[1], "american": "-114", "decimal": 1.8771929824}]}


class ReadinessBudgetTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        class Pending(RuntimeError):
            pass
        self.core = SimpleNamespace(FATAL_BROWSER_ERRORS=(InvalidSessionIdException,), WebDriverException=WebDriverException,
            BrowserNavigationPending=Pending, REVEAL_SCRIPT="cancel", reveal_set_winner=Mock(), open_props=Mock(return_value=True))
        self.driver = Mock()
        self.reader = MarketReadiness(self.core, clock=lambda: self.now, sleep=self.advance)

    def advance(self, duration):
        self.now += duration

    def test_first_loading_response_is_retried_instead_of_becoming_a_miss(self):
        self.core.reveal_set_winner.side_effect = [{"status": "waiting", "reason": "expanding winner section"}, {"status": "revealed"}]
        result = self.reader.wait(self.driver, EVENT, MARKET, lambda: True, lambda: 4.3-self.now)
        self.assertEqual(result["status"], "revealed")
        self.assertEqual(result["attempts"], 2)
        self.assertLess(self.now, .1)

    def test_ready_market_has_no_extra_sleep(self):
        self.core.reveal_set_winner.return_value = {"status": "revealed"}
        result = self.reader.wait(self.driver, EVENT, MARKET, lambda: True, lambda: 4.3-self.now)
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(self.now, 0)

    def test_expiry_or_stop_cancels_the_read_only_observer(self):
        self.core.reveal_set_winner.return_value = {"status": "waiting", "reason": "selection IDs missing or ambiguous"}
        result = self.reader.wait(self.driver, EVENT, MARKET, lambda: self.now < .05, lambda: .05-self.now, reserve=0)
        self.assertEqual(result["status"], "expired")
        self.driver.execute_script.assert_called_with("cancel", None)
        self.assertLessEqual(self.now, .05)

    def test_readiness_wait_keeps_a_dispatch_reserve(self):
        self.core.reveal_set_winner.return_value = {"status": "waiting", "reason": "winner section missing"}
        result = self.reader.wait(self.driver, EVENT, MARKET, lambda: self.now < 1, lambda: 1-self.now, reserve=.6)
        self.assertEqual(result["status"], "not_ready")
        self.assertAlmostEqual(self.now, .4)

    def test_transient_opening_failure_is_retried_without_requesting_long_navigation_timeouts(self):
        self.core.open_props.side_effect = [self.core.BrowserNavigationPending("rendering"), False, True]
        result = self.reader.open(self.driver, EVENT, MARKET, lambda: True, lambda: 4.3-self.now)
        self.assertEqual(result["status"], "opened")
        self.assertEqual(result["attempts"], 3)
        self.assertTrue(all(call.kwargs["timeout"] <= .4 for call in self.core.open_props.call_args_list))

    def test_browser_loss_is_never_retried(self):
        self.core.reveal_set_winner.side_effect = InvalidSessionIdException("closed")
        with self.assertRaises(InvalidSessionIdException):
            self.reader.wait(self.driver, EVENT, MARKET, lambda: True, lambda: 4)
        self.assertEqual(self.core.reveal_set_winner.call_count, 1)

    def test_readiness_turn_yields_without_cancelling_an_unexpired_expansion(self):
        self.core.reveal_set_winner.return_value = {'status': 'waiting', 'reason': 'expanding winner section'}
        result = self.reader.wait(self.driver, EVENT, MARKET, lambda: True, lambda: 5-self.now, max_wait=.35)
        self.assertEqual(result['status'], 'not_ready')
        self.assertAlmostEqual(self.now, .35)
        self.driver.execute_script.assert_not_called()

    def test_opening_turn_uses_a_bounded_budget(self):
        self.core.open_props.return_value = False
        result = self.reader.open(self.driver, EVENT, MARKET, lambda: True, lambda: 5-self.now, max_wait=.35)
        self.assertEqual(result['status'], 'not_ready')
        self.assertAlmostEqual(self.now, .35)
        self.assertTrue(all(call.kwargs['timeout'] <= .35 for call in self.core.open_props.call_args_list))

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = f'''<!doctype html><html><body>
        <div class="cms-event-scoreboard-container"><div>{PLAYERS[0]}</div><div>0 – 1</div><div>{PLAYERS[1]}</div></div>
        <button role="tab" aria-selected="true">Game Props</button>
        <div data-testid="collapsible-wrapper"><button id="expand" data-testid="collapsible-trigger" aria-expanded="false">
        <h2 data-testid="collapsible-header">Winner - Listed Game</h2></button><div id="rows"></div></div>
        <script>window.selectionClicks=0;window.submitClicks=0;window.expansions=0;window.rowDelay=250;
        document.addEventListener('click',event=>{{if(event.target.closest('[data-testid^="component-builder-market-button-"]'))window.selectionClicks++;}});
        document.getElementById('expand').onclick=()=>{{window.expansions++;setTimeout(()=>{{
            document.getElementById('expand').setAttribute('aria-expanded','true');window.insertRows();
        }},window.rowDelay)}};</script></body></html>'''.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class MarketReadinessBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1280,900")
        cls.driver = create_owned_chrome(options)

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.driver.get(f"http://127.0.0.1:{self.server.server_port}/event/oliver-vincenec-vs-lukasz-oracz/{EVENT_ID}")
        self.event, self.market = deepcopy(EVENT), deepcopy(MARKET)
        self.event["receivedAt"] = self.market["receivedAt"] = datetime.now(timezone.utc).isoformat()
        row = '<div data-testid="market-template"><div data-testid="market-label">Game 2</div>'
        for identifier, label, price in zip(SELECTIONS, PLAYERS, ("-124", "-114")):
            row += f'<button data-testid="component-builder-market-button-{EVENT_ID}-{identifier}" aria-pressed="false"><span data-testid="button-title-market-board">{label}</span><span data-testid="button-odds-market-board">{price}</span></button>'
        row += '</div>'
        self.driver.execute_script("window.fixtureRow=arguments[0];window.insertRows=()=>{document.getElementById('rows').innerHTML=window.fixtureRow};", row)
        def identity(driver, event):
            url = urlsplit(driver.current_url)
            return url.hostname == "127.0.0.1" and url.path.endswith("/"+event["eventId"]) and all(
                name in driver.find_element("css selector", ".cms-event-scoreboard-container").text for name in PLAYERS)
        # The executor resolves correct_event in its own namespace; MarketReadiness
        # also calls it directly on the module it was given. Patch both bindings.
        for patcher in (patch.object(execution, "correct_event", side_effect=identity),
                        patch.object(core, "correct_event", side_effect=identity)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.reader = MarketReadiness(core)

    def assert_no_wager_actions(self):
        self.assertEqual(self.driver.execute_script("return window.selectionClicks"), 0)
        self.assertEqual(self.driver.execute_script("return window.submitClicks"), 0)

    def test_reported_oliver_market_first_waits_then_recovers_when_async_expansion_finishes(self):
        first = core.reveal_set_winner(self.driver, self.event, self.market)
        self.assertEqual(first["status"], "waiting")
        self.assertEqual(first["reason"], "expanding winner section")
        deadline = time.monotonic()+4.3
        result = self.reader.wait(self.driver, self.event, self.market, lambda: time.monotonic() < deadline,
                                  lambda: deadline-time.monotonic())
        self.assertEqual(result["status"], "revealed")
        self.assertEqual(self.driver.execute_script("return window.expansions"), 1)
        self.assert_no_wager_actions()

    def test_idle_warmup_expands_the_section_without_selecting_odds(self):
        self.driver.execute_script("window.rowDelay=0")
        with patch("ember_market_readiness.PROVIDER_HOST", "127.0.0.1"):
            self.reader.warm(self.driver, self.event, {"betType": "split"})
            self.driver.execute_script("document.getElementById('expand').setAttribute('aria-expanded','true');window.insertRows()")
            result = self.reader.warm(self.driver, self.event, {"betType": "split"})
        self.assertEqual(result["status"], "ready")
        self.assertEqual(self.driver.execute_script("return window.expansions"), 1)
        self.assert_no_wager_actions()

    def distribution_view(self, name, header, row_label=None):
        from distribution_policy import MARKETS
        self.market = deepcopy(MARKET)
        self.market['receivedAt'] = self.event['receivedAt']
        self.market.update(name=name, betType=MARKETS[name])
        if name == 'Correct Score':
            self.market['outcomes'] = [
                {'selectionId': SELECTIONS[0]+'-'+str(index),
                 'label': f'{PLAYERS[0]} {a}:{b} {PLAYERS[1]}', 'american': '+250'}
                for index, (a, b) in enumerate(((3, 0), (3, 1), (3, 2), (0, 3), (1, 3), (2, 3)))]
        row = '<div data-testid="market-template">'
        if row_label:
            row += f'<div data-testid="market-label">{row_label}</div>'
        for outcome in self.market['outcomes']:
            row += (f'<button data-testid="component-builder-market-button-{EVENT_ID}-{outcome["selectionId"]}" aria-pressed="false">'
                    f'<span data-testid="button-title-market-board">{outcome["label"]}</span>'
                    '<span data-testid="button-odds-market-board">+250</span></button>')
        row += '</div>'
        self.driver.execute_script("""
            document.querySelector('[role=tab]').textContent='Match Lines';
            document.querySelector('[data-testid=collapsible-header]').textContent=arguments[0];
            window.fixtureRow=arguments[1];window.rowDelay=0;
        """, header, row)

    def test_feed_names_reveal_real_provider_sections_and_exact_selections(self):
        for name, header in (('Games Spread', 'Spread'), ('Points Spread', 'Spread'),
                             ('Correct Score', 'Correct Score BO5'), ('Total Points', 'Total')):
            with self.subTest(name=name, header=header):
                self.distribution_view(name, header)
                self.driver.execute_script("document.getElementById('expand').setAttribute('aria-expanded','false');document.getElementById('rows').innerHTML=''")
                deadline = time.monotonic()+2
                result = self.reader.wait(self.driver, self.event, self.market,
                    lambda: time.monotonic() < deadline, lambda: deadline-time.monotonic())
                self.assertEqual(result['status'], 'revealed', result)
                self.assertLess(result['wait_ms'], 1000)
                self.assert_no_wager_actions()

    def test_distribution_warmup_uses_the_same_provider_headers_even_without_a_feed_name(self):
        from distribution_policy import MARKETS
        for name, header in (('Games Spread', 'Spread'), ('Points Spread', 'Spread'),
                             ('Correct Score', 'Correct Score BO5'), ('Total Points', 'Total')):
            with self.subTest(name=name):
                self.distribution_view(name, header)
                self.driver.execute_script("document.getElementById('expand').setAttribute('aria-expanded','false')")
                with patch('ember_market_readiness.PROVIDER_HOST', '127.0.0.1'):
                    self.reader.warm(self.driver, self.event, {'betType': MARKETS[name]})
                    self.driver.execute_script("document.getElementById('expand').setAttribute('aria-expanded','true')")
                    result = self.reader.warm(self.driver, self.event, {'betType': MARKETS[name]})
                self.assertEqual(result['status'], 'ready', result)
                self.assert_no_wager_actions()

    def test_correct_score_popular_section_alias_avoids_unnecessary_tab_navigation(self):
        self.distribution_view('Correct Score', 'Correct Score BO5', row_label='Correct Score BO5')
        self.driver.execute_script("document.querySelector('[role=tab]').textContent='Popular';document.getElementById('expand').setAttribute('aria-expanded','true');window.insertRows()")
        result = core.reveal_set_winner(self.driver, self.event, self.market)
        self.assertEqual(result['status'], 'revealed', result)
        self.assert_no_wager_actions()

    def test_shared_spread_header_does_not_accept_a_different_markets_selection_ids(self):
        self.distribution_view('Points Spread', 'Spread')
        self.driver.execute_script("document.getElementById('expand').setAttribute('aria-expanded','true');window.insertRows()")
        self.market['outcomes'][0]['selectionId'] = 'different-spread-selection'
        result = core.reveal_set_winner(self.driver, self.event, self.market)
        self.assertEqual(result['status'], 'waiting')
        self.assertEqual(result['reason'], 'selection IDs missing or ambiguous')
        self.assert_no_wager_actions()

    def test_duplicate_provider_sections_remain_ambiguous(self):
        self.distribution_view('Correct Score', 'Correct Score BO5')
        self.driver.execute_script("document.body.appendChild(document.querySelector('[data-testid=collapsible-wrapper]').cloneNode(true))")
        result = core.reveal_set_winner(self.driver, self.event, self.market)
        self.assertEqual(result['status'], 'waiting')
        self.assertEqual(result['reason'], 'market section ambiguous')
        self.assert_no_wager_actions()

    def test_ambiguous_sections_are_not_accepted_by_retry(self):
        self.driver.execute_script("document.getElementById('expand').setAttribute('aria-expanded','true');window.insertRows();document.body.appendChild(document.querySelector('[data-testid=collapsible-wrapper]').cloneNode(true))")
        deadline = time.monotonic()+.6
        result = self.reader.wait(self.driver, self.event, self.market, lambda: time.monotonic() < deadline,
                                  lambda: deadline-time.monotonic(), reserve=.2)
        self.assertNotEqual(result["status"], "revealed")
        self.assertEqual(result["reason"], "winner section ambiguous")
        self.assert_no_wager_actions()

    def test_wrong_market_ids_never_become_ready(self):
        self.driver.execute_script("document.getElementById('expand').setAttribute('aria-expanded','true');window.insertRows()")
        self.market["outcomes"][0]["selectionId"] = "wrong-selection"
        deadline = time.monotonic()+.6
        result = self.reader.wait(self.driver, self.event, self.market, lambda: time.monotonic() < deadline,
                                  lambda: deadline-time.monotonic(), reserve=.2)
        self.assertNotEqual(result["status"], "revealed")
        self.assertEqual(result["reason"], "selection IDs missing or ambiguous")
        self.assert_no_wager_actions()

    def summary_view(self, exact_row=False):
        self.driver.execute_script("""
            document.getElementById('expand').setAttribute('aria-expanded','true');window.insertRows();
            const props=document.querySelector('[role=tab]');props.setAttribute('aria-selected','false');
            const summary=document.createElement('button');summary.setAttribute('role','tab');
            summary.setAttribute('aria-selected','true');summary.textContent='Popular';props.before(summary);
            window.tabClicks=0;
            props.onclick=event=>{if(event.isTrusted)window.tabClicks++;summary.setAttribute('aria-selected','false');
                props.setAttribute('aria-selected','true');window.insertRows();};
            if(!arguments[0]){document.getElementById('rows').innerHTML='<div data-testid="market-template"><div data-testid="market-label">Game 1</div></div>';}
        """, exact_row)

    def test_summary_view_with_wrong_game_row_recovers_to_game_props(self):
        self.summary_view()
        first = core.reveal_set_winner(self.driver, self.event, self.market)
        self.assertEqual(first["status"], "waiting")
        self.assertEqual(first["reason"], "selection IDs missing or ambiguous")
        deadline = time.monotonic()+4.3
        result = self.reader.wait(self.driver, self.event, self.market, lambda: time.monotonic() < deadline,
                                  lambda: deadline-time.monotonic())
        self.assertEqual(result["status"], "revealed")
        self.assertEqual(self.driver.execute_script("return window.tabClicks"), 1)
        self.assert_no_wager_actions()

    def test_summary_view_that_already_has_the_exact_row_is_not_switched(self):
        self.summary_view(exact_row=True)
        deadline = time.monotonic()+4.3
        result = self.reader.wait(self.driver, self.event, self.market, lambda: True, lambda: deadline-time.monotonic())
        self.assertEqual(result["status"], "revealed")
        self.assertEqual(self.driver.execute_script("return window.tabClicks"), 0)
        self.assert_no_wager_actions()

    def test_disabled_quotes_wait_for_a_real_enabled_update_without_clicking_them(self):
        self.driver.execute_script("""
            document.getElementById('expand').setAttribute('aria-expanded','true');window.insertRows();
            document.querySelectorAll('[data-testid^="component-builder-market-button-"]').forEach(button=>button.disabled=true);
            setTimeout(()=>document.querySelectorAll('[data-testid^="component-builder-market-button-"]').forEach(button=>button.disabled=false),250);
        """)
        first = core.reveal_set_winner(self.driver, self.event, self.market)
        self.assertEqual(first["status"], "waiting")
        deadline = time.monotonic()+4.3
        result = self.reader.wait(self.driver, self.event, self.market, lambda: time.monotonic() < deadline,
                                  lambda: deadline-time.monotonic())
        self.assertEqual(result["status"], "revealed")
        self.assert_no_wager_actions()


if __name__ == "__main__":
    unittest.main()
