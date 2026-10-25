"""Actual client FIFO + real DOM executor, served entirely by loopback HTTP."""
from contextlib import ExitStack, closing, contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from selenium.webdriver.chrome.options import Options

from driver_lifecycle import create_owned_chrome
from ember_client import Client, SignalQueue
from ember_protocol import timestamp, validate_signal
import test_ember_client as client_fixtures
from test_ember_slip import multi_leg_html
import tt_market_exec as execution
from wager_submission import settle_submission


def fixture_signals(h2h=False):
    signals = []
    for index, players in enumerate((('One', 'Two'), ('One', 'Two') if h2h else ('Three', 'Four'))):
        signal = client_fixtures.ProtocolV2Tests().plan_signal()
        event_id = str(100+index)
        signal.update(id=index+1, contract=event_id+':split')
        event, market, plan = (signal['payload'][key] for key in ('event', 'market', 'plan'))
        event.update(eventId=event_id, player1=players[0], player2=players[1],
                     receivedAt=datetime.now(timezone.utc).isoformat())
        market.update(eventId=event_id, marketId=str(200+index), receivedAt=event['receivedAt'])
        for row, player, suffix in zip(market['outcomes'], reversed(players), ('a', 'b')):
            row.update(selectionId=event_id+suffix, label=player)
        plan.update(selection=players[1], selectionId=event_id+'a')
        if h2h:
            rows = [{'set': number, 'home': 11 if number % 2 else 5, 'away': 5 if number % 2 else 11}
                    for number in range(1, 5)]+[{'set': 5, 'home': 0, 'away': 0}]
            event.update(sets=rows, currentSet=rows[-1], period='5th', setTally=[2, 2])
            market.update(name='Moneyline', scoreState=[2, 2], subcategoryId=6386)
            for outcome in market['outcomes']:
                outcome['selectionId'] = '0ML'+outcome['selectionId']
            plan.update(betType='closeout', nextSet=5, scoreState=[2, 2], selectionId='0ML'+event_id+'a',
                        specification={'family': 'closeout', 'player': 1, 'target': 5})
            signal['contract'] = event_id+':closeout'
        signals.append(validate_signal(signal))
    return signals


def fixture_stages(signals):
    now = datetime.now(timezone.utc)
    return [{'protocol': 1, 'event_id': s['payload']['event']['eventId'], 'event': s['payload']['event'],
             'market_id': s['payload']['market']['marketId'], 'market_name': s['payload']['market']['name'],
             'bet_type': s['payload']['plan']['betType'], 'selection_id': outcome['selectionId'],
             'label': outcome['label'], 'line': None, 'updated_at': now.isoformat(),
             'expires_at': (now+timedelta(seconds=10)).isoformat()}
            for s in signals for outcome in s['payload']['market']['outcomes']]


def scheduling_html(signals):
    contracts = []
    for signal in signals:
        event, market, plan = (signal['payload'][key] for key in ('event', 'market', 'plan'))
        contracts.append((event, market, {**plan, 'specification': {'line': None}},
                          {'label': plan['selection'], 'selectionId': plan['selectionId']}))
    fixture = [{'eventId': s['payload']['event']['eventId'],
                'players': [s['payload']['event']['player1'], s['payload']['event']['player2']],
                'name': s['payload']['market']['name'], 'outcomes': s['payload']['market']['outcomes']}
               for s in signals]
    return multi_leg_html(contracts)+'''
    <div class="cms-event-scoreboard-container">One vs Two</div>
    <button role="tab" aria-selected="true">Game Props</button>
    <div data-testid="collapsible-wrapper"><button data-testid="collapsible-trigger" aria-expanded="true">
    <h2 data-testid="collapsible-header">Winner - Listed Game</h2></button><div id="rows"></div></div>
    <script>
    const fixture=FIXTURE_DATA;
    document.querySelector('.cms-event-scoreboard-container').textContent=fixture[0].players.join(' vs ');
    document.querySelector('[role=tab]').textContent=fixture[0].name==='Moneyline'?'Match Lines':'Game Props';
    document.querySelector('[data-testid=collapsible-header]').textContent=fixture[0].name==='Moneyline'?'Moneyline':'Winner - Listed Game';
    window.__isLoggedIn=true;window.__canBet=true;
    window.__playSpan={playSpan:{clientKey:'fixture-native-grant',grantType:'PlaySportsbook'}};
    window.selectionClicks=[];window.fixtureSubmits=[];
    window.cards=[...document.querySelectorAll('section')].map(e=>e.outerHTML);
    const empty=document.createElement('div');empty.dataset.testid='betslip-empty-state-title';slip.append(empty);
    window.addLeg=(index,selectionId,label)=>{const card=new DOMParser().parseFromString(window.cards[index],'text/html').querySelector('section');
        const remove=card.querySelector('[data-tracking]'),tracking=JSON.parse(remove.getAttribute('data-tracking'));
        if(selectionId){tracking.selectionId=selectionId;remove.setAttribute('data-tracking',JSON.stringify(tracking));card.children[1].textContent=label;}
        if(![...slip.querySelectorAll('[data-tracking]')].some(e=>JSON.parse(e.getAttribute('data-tracking')).selectionId===tracking.selectionId))
            slip.prepend(card);};
    const originalPlace=button.onclick;
    button.onclick=()=>{window.fixtureSubmits.push({at:performance.now(),wall:Date.now()});originalPlace();
        if(window.releaseA)setTimeout(()=>document.querySelector('section input')?.removeAttribute('disabled'),80);};
    window.renderRows=index=>{const {eventId,outcomes,name}=fixture[index];
        const rows=document.getElementById('rows');rows.innerHTML='<div data-testid="market-template"><div data-testid="market-label">'+name+'</div></div>';
        for(const {selectionId,label} of outcomes){
            const b=document.createElement('button');b.dataset.testid='component-builder-market-button-'+eventId+'-'+selectionId;
            b.setAttribute('aria-pressed','false');b.innerHTML='<span data-testid="button-title-market-board">'+label+'</span><span data-testid="button-odds-market-board">+150</span>';
            b.onclick=event=>{window.selectionClicks.push({id:eventId,trusted:event.isTrusted});if(event.isTrusted){window.addLeg(index,selectionId,label);b.setAttribute('aria-pressed','true');}};
            rows.firstElementChild.append(b);
        }};
    for(let index=0;index<2;index++){
        const a=document.createElement('a');a.href='https://sportsbook.draftkings.com/event/fixture/'+(100+index);
        a.textContent='More Bets';a.onclick=event=>{event.preventDefault();if(!event.isTrusted)return;
            history.pushState(null,'','/event/fixture/'+(100+index));
            document.querySelector('.cms-event-scoreboard-container').textContent=fixture[index].players.join(' vs ');window.renderRows(index);};
        document.body.append(a);
    }
    </script>'''.replace('FIXTURE_DATA', json.dumps(fixture))


@contextmanager
def scheduling_server(signals):
    state = SimpleNamespace(html=scheduling_html(signals), calls=[], on_first=None, ledger=None, slow_authorization=False)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond(200, state.html.encode('utf-8'), 'text/html; charset=utf-8')

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            identifier = int(self.path.split('/')[-3])
            signal = signals[identifier-1]
            state.calls.append({'id': identifier, 'claim': body.get('claim'), 'at': time.monotonic()})
            if state.on_first is not None:
                callback, state.on_first = state.on_first, None
                callback()
            if identifier == 1 and not body.get('claim') and state.slow_authorization:
                state.slow_authorization = False
                time.sleep(.6)
            now = datetime.now(timezone.utc)
            if timestamp(signal['expires_at']) <= now.timestamp():
                self.respond(409, json.dumps({'error': 'signal_expired'}).encode())
                return
            response = {'enabled': True, 'client_supported': True, 'feed_healthy': True,
                        'server_time': now.isoformat(), 'access_until': (now+timedelta(hours=1)).isoformat(),
                        'lease_seconds': 5, 'signal': deepcopy({key: signal[key] for key in
                            ('protocol', 'id', 'contract', 'expires_at', 'payload')})}
            if body.get('claim'):
                with closing(sqlite3.connect(state.ledger)) as ledger:
                    reserved = ledger.execute('SELECT status FROM attempts WHERE contract=?', (signal['contract'],)).fetchone()
                if reserved != ('reserved_or_unconfirmed',):
                    self.respond(500, json.dumps({'error': 'missing_durable_reservation'}).encode())
                    return
                response['plan'] = {'stake_cents': 261, 'min_decimal': 1.85, 'echo': 'fixture-claim'}
            self.respond(200, json.dumps(response).encode())

        def respond(self, status, body, content_type='application/json'):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *args):
            pass
    class FixtureServer(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            if not isinstance(sys.exc_info()[1], ConnectionError):
                super().handle_error(request, client_address)
    server = FixtureServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def local_identity(driver, event):
    address = urlsplit(driver.current_url)
    if address.hostname != '127.0.0.1' or address.path.rstrip('/').split('/')[-1] != event['eventId']:
        return False
    boards = driver.find_elements('css selector', '.cms-event-scoreboard-container')
    return any(board.is_displayed() and all(event[key] in board.text for key in ('player1', 'player2')) for board in boards)


class ClientSchedulingBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        options = Options()
        options.page_load_strategy = 'none'
        options.add_argument('--headless=new')
        options.add_argument('--window-size=1400,1000')
        cls.driver = create_owned_chrome(options)
        cls.addClassCleanup(cls.driver.quit)
        cls.evidence_dir = Path(tempfile.mkdtemp(prefix='ember-scheduling-'))/'output'
        cls.evidence_dir.mkdir()

    def run_schedule(self, mode='ready', reverse=False):
        signals = fixture_signals(h2h=True)
        first, second = (1, 0) if reverse else (0, 1)
        with ExitStack() as stack:
            directory = stack.enter_context(tempfile.TemporaryDirectory())
            stack.enter_context(patch.dict(os.environ, {'LOCALAPPDATA': directory,
                'EMBER_DEVICE_TEST_ONCE': '', 'EMBER_DEVICE_TEST_LOGIN_ONLY': '',
                'EMBER_DEVICE_TEST_SCAN_ONLY': '', 'EMBER_DEVICE_BROWSER_TICKET': ''}))
            stack.enter_context(patch.dict(sys.modules, {'keyring': Mock()}))
            client = Client()
            stack.callback(client.history_wake.clear)
            client.lease.running = True
            client.last_feed = True
            client.account = 'fixture-account'
            client.access_token, client.token_until = 'synthetic-fixture-token', float('inf')
            server, state = stack.enter_context(scheduling_server(signals))
            client.api = f'http://127.0.0.1:{server.server_port}'
            state.ledger = client.data_dir/'execution.sqlite3'
            state.slow_authorization = mode == 'slow-authorization'
            now = datetime.now(timezone.utc)
            client.lease.update({'enabled': True, 'client_supported': True, 'server_time': now.isoformat(),
                'access_until': (now+timedelta(hours=1)).isoformat(), 'lease_seconds': 5})
            driver = self.driver
            driver._tt_plan_uncertain = driver._tt_plan_stopped = False
            driver.get(client.api+'/event/one-vs-two/100')
            from selenium.webdriver.support.ui import WebDriverWait
            WebDriverWait(driver, 5).until(lambda d: d.execute_script(
                'return location.port===arguments[0]&&typeof window.renderRows==="function"', str(server.server_port)))
            if mode in ('slow', 'missing'):
                driver.execute_script('document.querySelector("section").remove()')
                if mode == 'slow':
                    driver.execute_script('setTimeout(()=>window.renderRows(0),800)')
            elif mode == 'suspended':
                driver.execute_script('document.querySelector("section input").disabled=true;window.releaseA=true')
            elif mode == 'quote-suspended':
                real_async = driver.execute_async_script
                suspended = []
                def quote_transition(script, *args):
                    if script == execution.WAIT_FOR_PLAN_ARM_SCRIPT and args[0].get('readOnly') and args[0].get('eventId') == '100' and not suspended:
                        suspended.append(True)
                        driver.execute_script('const input=document.querySelector("section input");input.disabled=true;window.releaseA=true;'
                                              'if(arguments[0])setTimeout(()=>input.disabled=false,250)', reverse)
                    return real_async(script, *args)
                stack.enter_context(patch.object(driver, 'execute_async_script', side_effect=quote_transition))
            elif mode == 'uncertain':
                driver.execute_script("window.mode='silent'")
            elif mode == 'stale':
                signals[0]['expires_at'] = signals[0]['payload']['plan']['deadline'] = (now-timedelta(seconds=1)).isoformat()
            elif mode == 'staging':
                driver.execute_script('document.querySelectorAll("section").forEach(e=>e.remove());window.renderRows(0)')
                client.scanner = {'stages': fixture_stages(signals)}
                for stage in client.scanner['stages']:
                    event = deepcopy(stage['event'])
                    rows = event['sets'][:1]+[{'set': 2, 'home': 3, 'away': 2}]
                    event.update(sets=rows, currentSet=rows[-1], setTally=[1, 0], period='2nd')
                    stage['event'] = event
            if mode in ('ready', 'simultaneous'):
                driver.execute_script('document.querySelector(".cms-event-scoreboard-container").remove();document.querySelector("[data-testid=collapsible-wrapper]").remove()')
            def enqueue(index):
                if not any(s.get('_timing') for s in signals):
                    deadline = (datetime.now(timezone.utc)+timedelta(seconds=5)).isoformat()
                    for s in signals:
                        if mode != 'stale' or s['id'] != 1:
                            s['expires_at'] = s['payload']['plan']['deadline'] = deadline
                client.receive_opportunity(signals[index], client.history_scope())
                client.signals.put_latest(signals[index])
            if mode != 'staging':
                enqueue(first)
            if mode == 'simultaneous':
                enqueue(second)
            else:
                state.on_first = lambda: enqueue(second)
            self.assertIsInstance(client.signals, SignalQueue)
            owner = SimpleNamespace(open=lambda active: driver, detach=lambda: None, retained=False)
            gate = Mock()
            gate.poll.return_value = {'ready': True, 'signed_in': True, 'signed_out_confirmed': False,
                'provider_can_bet': True, 'native_wagering_grant': True, 'location': {'state': 'ready', 'blocked': False}}
            def ready_provider(*args):
                now = datetime.now(timezone.utc)
                client.lease.update({'enabled': True, 'client_supported': True, 'server_time': now.isoformat(),
                    'access_until': (now+timedelta(hours=1)).isoformat(), 'lease_seconds': 5})
                if mode == 'staging':
                    snapshot = execution.read_plan_slip(driver)
                    if len(snapshot['legs']) == 4 and not signals[first].get('_timing'):
                        self.assertTrue(snapshot['stagingSafe'])
                        self.assertEqual(driver.execute_script('return window.inputEvents'), 0)
                        self.assertEqual(driver.execute_script('return window.actions'), [])
                        enqueue(first)
                return gate.poll.return_value
            gate.poll.side_effect = ready_provider
            output = io.StringIO()
            request_trace, request = [], client.request
            def timed_request(path, body=None, **kwargs):
                reading = {'claim': bool((body or {}).get('claim')), 'timeout': kwargs.get('timeout'),
                           'started': time.monotonic()}
                request_trace.append(reading)
                try:
                    return request(path, body, **kwargs)
                except Exception as error:
                    reading['error_type'] = type(error).__name__
                    raise
                finally:
                    reading['finished'] = time.monotonic()
            for setting in (patch('ember_client.OUTPUT', output),
                patch.object(client, 'request', side_effect=timed_request),
                patch('draftkings_login.allow_location_permissions'), patch('draftkings_login.goto_tt_elite'),
                patch('ember_provider_login.ProviderLoginGate', return_value=gate),
                patch('draftkings_session.SessionMaintenance'),
                patch.object(execution, 'correct_event', side_effect=local_identity),
                patch.object(execution, 'event_route_open', side_effect=lambda d, e: urlsplit(d.current_url).path.endswith('/'+e['eventId'])),
                patch.object(execution, 'settle_submission', side_effect=lambda *args: settle_submission(*args, timeout=.3))):
                stack.enter_context(setting)
            real_get = driver.get
            def local_get(url):
                self.assertTrue(url.startswith(client.api+'/'), 'fixture attempted nonlocal navigation')
                return real_get(url)
            stack.enter_context(patch.object(driver, 'get', side_effect=local_get))
            started = time.monotonic()
            client.execute_browser(owner, lambda: client.lease.running and time.monotonic()-started < 8 and
                not all('finished' in signal.get('_timing', {}) for signal in signals))
            actions = driver.execute_script('return window.actions')
            dispatches = driver.execute_script('return window.fixtureSubmits')
            history = client.history.snapshot(client.history_scope(), outcome=None)
            with closing(sqlite3.connect(state.ledger)) as ledger:
                reserved = ledger.execute('SELECT contract FROM attempts ORDER BY contract').fetchall()
            evidence = {'mode': mode, 'reverse': reverse, 'matches': [
                {'event_id': s['payload']['event']['eventId'], **{key: s.get('_timing', {}).get(key)
                    for key in ('received', 'dequeued', 'submitted', 'finished')}} for s in signals],
                'actions': actions, 'dispatches': dispatches, 'authorizations': state.calls, 'requests': request_trace,
                'outcomes': history['records'], 'inventory': execution.read_plan_slip(driver)}
            path = self.evidence_dir/f'{mode}-{reverse}.json'
            path.write_text(json.dumps(evidence, indent=2), encoding='utf-8')
            latencies = {row['event_id']: round(1000*(row['submitted']-row['received']), 1)
                         if row['submitted'] else None for row in evidence['matches']}
            print(f'SCHEDULING {mode} reverse={reverse} receive_to_submit_ms={latencies} evidence={path}')
            self.assertNotIn(client.access_token, output.getvalue()+path.read_text(encoding='utf-8')+json.dumps(history))
            for signal in signals:
                timing = signal.get('_timing', {})
                if timing.get('submitted'):
                    self.assertLess(timing['submitted']-timing['received'], 5)
            for action, dispatch in zip(actions, dispatches):
                signal = next(s for s in signals if s['payload']['event']['eventId'] == action[0]['tracking']['eventId'])
                self.assertLess(dispatch['wall']/1000, min(timestamp(signal['expires_at']), timestamp(signal['payload']['plan']['deadline'])))
            return signals, actions, history, reserved, state.calls

    def test_overlapping_h2h_both_staged_execute_in_either_fifo_order_without_any_scoreboard(self):
        for mode in ('simultaneous', 'ready'):
            for reverse in (False, True):
                with self.subTest(mode=mode, reverse=reverse):
                    _, actions, history, _, calls = self.run_schedule(mode, reverse=reverse)
                    self.assertEqual([action[0]['tracking']['eventId'] for action in actions],
                                     ['101', '100'] if reverse else ['100', '101'])
                    self.assertEqual(history['counts'], {'placed': 2})
                    self.assertEqual(len(calls), 4)

    def test_overlapping_h2h_slow_board_and_suspended_slip_yield_then_both_place(self):
        for mode in ('slow', 'suspended', 'quote-suspended', 'slow-authorization'):
            for reverse in (False, True):
                with self.subTest(mode=mode, reverse=reverse):
                    signals, actions, history, _, _ = self.run_schedule(mode, reverse=reverse)
                    self.assertEqual([action[0]['tracking']['eventId'] for action in actions], ['101', '100'])
                    self.assertEqual(history['counts'], {'placed': 2})
                    self.assertLess(signals[1]['_timing']['dequeued']-signals[0]['_timing']['dequeued'], 2)

    def test_overlapping_h2h_missing_or_stale_a_cannot_reserve_or_submit_while_b_places(self):
        for mode in ('missing', 'stale'):
            for reverse in (False, True):
                with self.subTest(mode=mode, reverse=reverse):
                    signals, actions, history, reserved, _ = self.run_schedule(mode, reverse=reverse)
                    self.assertEqual([action[0]['tracking']['eventId'] for action in actions], ['101'])
                    self.assertEqual(reserved, [('101:closeout',)])
                    self.assertEqual(history['counts'], {'placed': 1, 'missed': 1})
                    self.assertLess(signals[1]['_timing']['dequeued']-signals[0]['_timing']['dequeued'], 2)

    def test_overlapping_h2h_uncertain_a_fences_b_and_is_never_replayed(self):
        signals, actions, history, reserved, calls = self.run_schedule('uncertain')
        self.assertEqual(len(actions), 1)
        self.assertEqual(reserved, [('100:closeout',)])
        self.assertNotIn('submitted', signals[1]['_timing'])
        self.assertTrue(self.driver._tt_plan_uncertain)
        self.assertEqual([call['id'] for call in calls], [1, 1])
        self.assertEqual(history['counts'].get('uncertain'), 1)

    def test_client_idle_scheduler_stages_pre_window_h2h_legs_before_two_live_signals(self):
        _, actions, history, _, _ = self.run_schedule('staging')
        self.assertEqual(len(actions), 2)
        self.assertEqual(history['counts'], {'placed': 2})
        snapshot = execution.read_plan_slip(self.driver)
        self.assertEqual({leg['selectionId'] for leg in snapshot['legs']}, {'0ML100b', '0ML101b'})
        self.assertTrue(snapshot['stagingSafe'])
        self.assertTrue(all(click['trusted'] for click in self.driver.execute_script('return window.selectionClicks')))


if __name__ == '__main__':
    unittest.main()
