"""Exact staged-leg contracts and local-HTTP, real-Chrome execution fixtures."""
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import unittest
from unittest.mock import Mock, patch

from selenium.webdriver.chrome.options import Options

import tt_market_exec as execution
from driver_lifecycle import create_owned_chrome, DriverConnectionLost
from ember_slip import plan_slip_config
from wager_submission import settle_submission


def contract(index=0):
    event_id, market_id, selection_id, name, line = (
        ("10", "m2", "s1", "Games Spread", 1.5),
        ("10", "m3", "s2", "Points Spread", -1.5),
        ("11", "m2", "s3", "Games Spread", 1.5))[index]
    event = {"eventId": event_id, "player1": "Piotr Zemla", "player2": "Tomasz Lojtek"}
    market = {"eventId": event_id, "marketId": market_id, "name": name}
    plan = {"selectionId": selection_id, "selection": "Tomasz Lojtek", "betType": "set_handicap",
            "nextSet": None, "scoreState": [1, 0], "specification": {"line": line},
            "deadline": (datetime.now(timezone.utc)+timedelta(seconds=20)).isoformat()}
    outcome = {"selectionId": selection_id, "label": "Tomasz Lojtek"}
    return event, market, plan, outcome


def h2h_contract(index=0):
    event, market, plan, outcome = contract(0 if index == 0 else 2)
    market.update(marketId='h2h', name='Moneyline')
    plan.update(betType='closeout', scoreState=[2, 0], specification={'line': None})
    return event, market, plan, outcome


def multi_leg_html(contracts=None):
    cards = []
    for index, (event, market, plan, outcome) in enumerate(contracts or [contract(i) for i in range(3)]):
        tracking = json.dumps({"eventId": event["eventId"], "marketId": market["marketId"],
                               "selectionId": outcome["selectionId"]})
        line = plan['specification']['line']
        line = '' if line is None else f'{line:+}'
        cards.append(f'''<section><div><button data-testid="betslip-selection-card-ex-button"
            data-tracking='{tracking}'>Remove</button></div><div>{outcome['label']}</div>
            <div>{market['name']}</div><span class="line">{line}</span>
             <span data-testid="betslip-odds-standard">{('+110', '+150', '+180')[index % 3]}</span>
            <input data-testid="betslip-wager-box-input" name="stake" inputmode="decimal" value=""></section>''')
    return ('''<!doctype html><meta charset="utf-8"><button id="standardized-header-account-menu">Account</button>
        <span id="standardized-header-balance">$100.00</span><div data-testid="betslip-content-wrapper">'''
        + ''.join(cards) + '''<div>Parlay <input data-testid="betslip-wager-box-input" inputmode="decimal" value=""></div>
        <button data-testid="betslip-place-bet-button">Place Bet</button></div><script>
        window.actions=[];window.mode='receipt';window.phase=0;window.inputEvents=0;
        window.__isLoggedIn=true;window.__canBet=true;
        window.__playSpan={playSpan:{clientKey:'fixture-native-grant',grantType:'PlaySportsbook'}};
        const slip=document.querySelector('[data-testid=betslip-content-wrapper]');
        const button=slip.querySelector('[data-testid=betslip-place-bet-button]');
        slip.addEventListener('input',()=>window.inputEvents++);
        button.onclick=()=>{
            const active=[...slip.querySelectorAll('input')].filter(e=>Number(e.value)!==0);
            window.actions.push(active.map(e=>({stake:e.value,tracking:JSON.parse(e.closest('section')
                ?.querySelector('[data-tracking]')?.getAttribute('data-tracking')||'null')})));
            if(window.mode==='silent')return;
            if(window.mode==='accept-only'&&window.phase++===0){button.textContent='Place Bet';return;}
            if(window.mode==='price-change'){
                active[0].closest('section').querySelector('[data-testid=betslip-odds-standard]').textContent='+120';
                button.textContent='Accept Odds Changes';return;}
            if(active.length!==1||!active[0].closest('section'))throw new Error('wrong exposure');
            const card=active[0].closest('section'),tracking=card.querySelector('[data-tracking]').getAttribute('data-tracking');
            card.remove();const receipt=document.createElement('div');receipt.dataset.testid='dk-betslip-receipt';
            receipt.textContent='Bet placed '+tracking;slip.appendChild(receipt);
        };
        </script>''')


@contextmanager
def slip_fixture_server():
    """Local provider-testids fixture; /h2h has overlapping identical-name matches."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            contracts = [h2h_contract(i) for i in range(2)] if self.path == '/h2h' else None
            body = multi_leg_html(contracts).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


class SlipConfigTests(unittest.TestCase):
    def test_server_deadline_is_translated_to_the_browser_clock_without_extending_it(self):
        event, market, plan, outcome=contract()
        plain=plan_slip_config(event,market,plan,outcome)
        ahead=plan_slip_config(event,market,plan,outcome,server_offset=10)
        behind=plan_slip_config(event,market,plan,outcome,server_offset=-10)
        self.assertEqual(ahead['validUntil'],plain['validUntil']-10000)
        self.assertEqual(behind['validUntil'],plain['validUntil']+10000)
    def test_config_carries_full_identity_line_and_timezone_deadline(self):
        config = plan_slip_config(*contract()[:3], contract()[3])
        self.assertEqual({k: config[k] for k in ('eventId', 'marketId', 'selectionId', 'selectionLine')},
                         {'eventId': '10', 'marketId': 'm2', 'selectionId': 's1', 'selectionLine': 1.5})
        self.assertGreater(config['validUntil'], datetime.now(timezone.utc).timestamp()*1000)

    def test_mismatched_or_malformed_contract_never_reaches_browser(self):
        for kind in ('event', 'selection', 'label', 'deadline', 'line', 'specification', 'market_name', 'empty_outcome'):
            with self.subTest(kind=kind):
                event, market, plan, outcome = contract()
                if kind == 'event':
                    market['eventId'] = 'other'
                elif kind == 'selection':
                    outcome['selectionId'] = 'other'
                elif kind == 'label':
                    outcome['label'] = 'other'
                elif kind == 'deadline':
                    plan['deadline'] = '2026-10-25T12:00:00'
                elif kind == 'specification':
                    plan['specification'] = []
                elif kind == 'market_name':
                    market['name'] = True
                elif kind == 'empty_outcome':
                    outcome = {}
                else:
                    plan['specification']['line'] = float('nan')
                driver = Mock()
                result = execution.place_plan(driver, event, market, plan, outcome, Mock())
                self.assertEqual(result['status'], 'skipped')
                driver.execute_script.assert_not_called()
                driver.execute_async_script.assert_not_called()


class MultiLegBrowserTests(unittest.TestCase):
    def test_expiry_during_final_validation_never_clicks_submit(self):
        event,market,plan,outcome=contract()
        config=plan_slip_config(event,market,plan,outcome)
        config.update(stake=2.61,minDecimal=1.85)
        self.js('document.querySelector("section input").value="2.61"')
        script=execution.CONFIRM_PLAN_SCRIPT.replace('const submittedAt =',
            'const until=performance.now()+700;while(performance.now()<until){};const submittedAt =')
        result=self.driver.execute_script('arguments[0].validUntil=Date.now()+500;\n'+script,config)
        self.assertEqual(result['status'],'aborted')
        self.assertIn('deadline',result['reason'])
        self.assertEqual(self.js('return window.actions'),[])

    def test_permission_lost_after_validation_never_clicks_submit(self):
        event,market,plan,outcome=contract()
        config=plan_slip_config(event,market,plan,outcome)
        config.update(stake=2.61,minDecimal=1.85)
        self.js('document.querySelector("section input").value="2.61"')
        script=execution.CONFIRM_PLAN_SCRIPT.replace('const submittedAt =','window.__canBet=false;const submittedAt =')
        result=self.driver.execute_script(script,config)
        self.assertEqual(result['status'],'aborted')
        self.assertEqual(self.js('return window.actions'),[])
    @classmethod
    def setUpClass(cls):
        resources = ExitStack()
        cls.addClassCleanup(resources.close)
        cls.server = resources.enter_context(slip_fixture_server())
        options = Options()
        options.add_argument('--headless=new')
        options.add_argument('--window-size=1400,900')
        cls.driver = create_owned_chrome(options)
        cls.addClassCleanup(cls.driver.quit)

    def setUp(self):
        self.reset()
        for setting in (patch.object(execution, 'save_slip_capture'),
                        patch.object(execution, 'SLIP_ARM_TIMEOUT', .5),
                        patch.object(execution, 'settle_submission',
                                     side_effect=lambda *args: settle_submission(*args, timeout=.6))):
            setting.start()
            self.addCleanup(setting.stop)

    def reset(self, h2h=False):
        self.driver._tt_plan_uncertain = False
        self.driver._tt_plan_stopped = False
        self.driver.get(f'http://127.0.0.1:{self.server.server_port}/'+('h2h' if h2h else 'fixture'))

    def js(self, script, *args):
        return self.driver.execute_script(script, *args)

    def run_plan(self, index=0, authorize=None, validate=None, plan_override=None, *, h2h=False, deadline_budget=None):
        event, market, plan, outcome = (h2h_contract if h2h else contract)(index)
        plan.update(plan_override or {})
        with ExitStack() as stack:
            for name in ('click_selection', 'clear_slip', 'open_props', 'correct_event'):
                stack.enter_context(patch.object(execution, name, side_effect=AssertionError('board dependency')))
            return execution.place_plan(self.driver, event, market, plan, outcome,
                authorize or (lambda quote: {'stake': 2.61, 'min_decimal': 2.0, 'echo': 'fixture'}), validate,
                deadline_budget=deadline_budget)

    def assert_unstaked(self):
        self.assertTrue(self.js("return [...document.querySelectorAll('input')].every(e=>Number(e.value)===0)"))

    def native_tracking(self):
        self.js("""for(const card of document.querySelectorAll('section')){
            card.dataset.testid='single-card';
            const remove=card.querySelector('[data-tracking]');
            const selection=JSON.parse(remove.getAttribute('data-tracking'));
            remove.setAttribute('data-tracking',JSON.stringify({event:'click',target:'Remove pick',
                screen:'dk-web-betslip',selectionId:selection.selectionId}));
            card.querySelector('.line').__reactFiber$fixture={memoizedProps:{selection},return:null};
        }""")

    def test_native_selection_only_tracking_uses_scoped_provider_identity_and_executes_exact_leg(self):
        self.native_tracking()
        snapshot = execution.read_plan_slip(self.driver)
        self.assertEqual(snapshot['status'], 'present')
        self.assertEqual([(r['eventId'],r['marketId'],r['selectionId']) for r in snapshot['legs']],
                         [('10','m2','s1'),('10','m3','s2'),('11','m2','s3')])
        self.assertEqual(self.js('return window.actions.length'), 0)
        result = self.run_plan(1)
        self.assertEqual(result['status'], 'placed')
        self.assertEqual(self.js('return window.actions[0].length'), 1)
        self.assertEqual(self.js('return window.actions[0][0].tracking.selectionId'), 's2')
        self.assert_unstaked()

    def suspend_without_controls(self):
        self.native_tracking()
        self.js("""const card=document.querySelector('section'),props=card.querySelector('.line').__reactFiber$fixture.memoizedProps;
            props.selection.status='suspended';props.bet={type:'Single',numberOfBets:1,selectionsMapped:[props.selection.selectionId],stake:null};
            card.querySelector('input').remove();card.querySelector('[data-testid="betslip-odds-standard"]').remove();
            card.append(document.createTextNode('SUSPENDED'));""")

    def test_native_suspended_unfunded_leg_without_controls_cannot_starve_ready_b(self):
        self.suspend_without_controls()
        snapshot=execution.read_plan_slip(self.driver)
        self.assertEqual(snapshot['status'],'present')
        self.assertTrue(snapshot['stagingSafe'])
        self.assertTrue(snapshot['legs'][0]['suspended'])
        self.assertIsNone(snapshot['legs'][0]['decimalOdds'])
        authorize=Mock()
        self.assertEqual(self.run_plan(0,authorize=authorize)['submissionState'],'not-submitted')
        authorize.assert_not_called()
        self.assertEqual(self.run_plan(1)['status'],'placed')
        self.assertEqual(self.js('return window.actions.length'),1)
        self.assertEqual(self.js('return window.actions[0][0].tracking.selectionId'),'s2')
        self.assert_unstaked()

    def test_unmounted_suspended_card_requires_positive_exact_unfunded_bet_evidence(self):
        for change in ("delete props.bet.stake", "props.bet.stake=1", "props.bet.stake=undefined",
                       "props.bet.selectionsMapped=['other']", "props.bet.numberOfBets=2", "props.bet.type='Parlay'",
                       "props.selection.status='unknown'"):
            with self.subTest(change=change):
                self.reset();self.suspend_without_controls()
                self.js("const props=document.querySelector('section .line').__reactFiber$fixture.memoizedProps;"+change)
                self.assertEqual(execution.read_plan_slip(self.driver)['status'],'unsupported')
                authorize=Mock()
                self.assertEqual(self.run_plan(1,authorize=authorize)['submissionState'],'not-submitted')
                authorize.assert_not_called()
                self.assertEqual(self.js('return window.actions.length'),0)

    def test_native_identity_never_guesses_missing_conflicting_or_external_evidence(self):
        mutations = (
            "delete card.querySelector('.line').__reactFiber$fixture",
            "delete record.marketId",
            "record.selectionId='another-selection'",
            "record.eventId=true",
            "card.querySelector('input').__reactFiber$other={memoizedProps:{selection:{...record,eventId:'other'}},return:null}",
            "remove.setAttribute('data-tracking',JSON.stringify({...record,eventId:'other'}))",
            "card.querySelector('.line').__reactFiber$fixture={stateNode:document.body,memoizedProps:{selection:record},return:null}",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.reset()
                self.native_tracking()
                self.js("const card=document.querySelector('section'),remove=card.querySelector('[data-tracking]'),record=card.querySelector('.line').__reactFiber$fixture.memoizedProps.selection;"+mutation)
                authorize = Mock()
                self.assertEqual(execution.read_plan_slip(self.driver)['status'], 'unsupported')
                self.assertEqual(self.run_plan(authorize=authorize)['submissionState'], 'not-submitted')
                authorize.assert_not_called()
                self.assertEqual(self.js('return window.actions.length'), 0)
                self.assert_unstaked()

    def test_provider_load_error_appearing_after_claim_aborts_without_a_submit_or_replay(self):
        def authorize(quote):
            self.js("const toast=document.createElement('div');toast.setAttribute('role','alert');toast.textContent=\"Oops-something didn't load right. We're on it. Try again in a sec.\";document.body.appendChild(toast)")
            return {'stake':2.61,'min_decimal':2.0,'echo':'fixture'}
        result = self.run_plan(authorize=authorize)
        self.assertEqual(result['submissionState'], 'not-submitted')
        self.assertIn('provider load error', result['reason'])
        self.assertEqual(self.js('return window.actions.length'), 0)
        self.assert_unstaked()

    def test_snapshot_and_ready_helpers_never_write_stakes_or_click(self):
        snapshot = execution.read_plan_slip(self.driver)
        self.assertTrue(snapshot['stagingSafe'])
        self.assertEqual([leg['selectionId'] for leg in snapshot['legs']], ['s1', 's2', 's3'])
        for index in range(3):
            self.assertTrue(execution.plan_slip_ready(self.driver, *contract(index)[:3]))
        self.assertEqual(self.js('return window.inputEvents'), 0)
        self.assertEqual(self.js('return window.actions'), [])
        self.assert_unstaked()

    def test_any_exact_leg_including_nonfirst_same_player_other_market_or_event(self):
        for index, expected_decimal in enumerate((2.1, 2.5, 2.8)):
            with self.subTest(index=index):
                self.reset()
                quoted = []
                def authorize(quote):
                    quoted.append(quote)
                    self.assert_unstaked()
                    return {'stake': 2.61, 'min_decimal': 2.0}
                result = self.run_plan(index, authorize)
                self.assertEqual(result['status'], 'placed', result)
                self.assertAlmostEqual(quoted[0]['decimalOdds'], expected_decimal)
                actions = self.js('return window.actions')
                self.assertEqual(len(actions), 1)
                self.assertEqual(len(actions[0]), 1)
                self.assertEqual(actions[0][0]['tracking']['selectionId'], contract(index)[3]['selectionId'])
                self.assertEqual(actions[0][0]['stake'], '2.61')
                self.assertEqual(len(execution.read_plan_slip(self.driver)['legs']), 2)
                self.assert_unstaked()

    def test_two_sequential_matches_each_submit_once_with_old_receipts_and_other_legs_remaining(self):
        self.assertEqual(self.run_plan(0)['status'], 'placed')
        self.assertTrue(execution.plan_slip_ready(self.driver, *contract(2)[:3]))
        self.assertEqual(self.run_plan(2)['status'], 'placed')
        self.assertEqual([row[0]['tracking']['eventId'] for row in self.js('return window.actions')], ['10', '11'])
        self.assertEqual([leg['selectionId'] for leg in execution.read_plan_slip(self.driver)['legs']], ['s2'])
        self.assert_unstaked()

    def test_non_target_single_and_parlay_stakes_including_hidden_or_invalid_never_submit(self):
        for position, value, hidden in ((1, '1', False), (3, '1', False), (1, '1', True),
                                        (3, 'NaN', False), (1, '-1', False)):
            with self.subTest(position=position, value=value, hidden=hidden):
                self.reset()
                self.js("const e=document.querySelectorAll('input')[arguments[0]];e.value=arguments[1];"
                        "if(arguments[2])e.style.display='none'", position, value, hidden)
                claim = Mock()
                self.assertEqual(self.run_plan(authorize=claim)['status'], 'skipped')
                claim.assert_not_called()
                self.assertFalse(execution.plan_slip_ready(self.driver, *contract()[:3]))
                self.assertEqual(self.js('return window.actions'), [])

    def test_unsupported_hidden_ambiguous_or_malformed_legs_fail_closed(self):
        changes = (
            "document.querySelector('[data-tracking]').setAttribute('data-tracking','{bad')",
            "document.querySelector('[data-tracking]').setAttribute('data-tracking',JSON.stringify({selectionId:'s1'}))",
            "document.querySelector('section').insertAdjacentHTML('afterend',document.querySelector('section').outerHTML)",
            "document.querySelector('section').style.display='none'",
            "document.querySelector('[data-testid=betslip-odds-standard]').style.display='none'",
            "document.querySelector('section input').insertAdjacentHTML('afterend','<input data-testid=betslip-wager-box-input>')",
            "document.querySelector('[data-tracking]').remove()",
            "document.querySelectorAll('input').forEach(e=>e.id='duplicate')",
            "document.querySelector('[data-tracking]').dataset.tracking=JSON.stringify({eventId:true,marketId:'m2',selectionId:'s1'})",
        )
        for change in changes:
            with self.subTest(change=change):
                self.reset()
                self.js(change)
                claim = Mock()
                self.assertEqual(self.run_plan(authorize=claim)['status'], 'skipped')
                claim.assert_not_called()
                self.assertEqual(self.js('return window.actions'), [])
                self.assert_unstaked()

    def test_changed_line_sign_price_or_identity_during_claim_never_arms(self):
        for change in ("document.querySelector('.line').textContent='-1.5'",
                       "document.querySelector('[data-testid=betslip-odds-standard]').textContent='+120'",
                       "const e=document.querySelector('[data-tracking]');const t=JSON.parse(e.dataset.tracking);"
                       "t.marketId='other';e.dataset.tracking=JSON.stringify(t)",
                       "document.querySelector('section').setAttribute('aria-disabled','true')"):
            with self.subTest(change=change):
                self.reset()
                def authorize(quote):
                    self.js(change)
                    return {'stake': 2.61, 'min_decimal': 2.0}
                self.assertEqual(self.run_plan(authorize=authorize)['status'], 'skipped')
                self.assertEqual(self.js('return window.actions'), [])
                self.assert_unstaked()

    def test_last_instant_change_cleans_owned_input_even_if_identity_changed(self):
        changes = ("document.querySelector('.line').textContent='-1.5'",
                   "document.querySelector('[data-testid=betslip-odds-standard]').textContent='+120'",
                   "document.querySelector('[data-tracking]').dataset.tracking=JSON.stringify({eventId:'99',marketId:'m2',selectionId:'s1'})",
                   "document.querySelector('section input').disabled=true",
                   "document.querySelector('[data-testid=betslip-place-bet-button]').textContent='Log In'")
        execute = self.driver.execute_script
        for change in changes:
            with self.subTest(change=change):
                self.reset()
                def dispatch(script, *args):
                    if script is execution.CONFIRM_PLAN_SCRIPT:
                        execute(change)
                    return execute(script, *args)
                with patch.object(self.driver, 'execute_script', side_effect=dispatch):
                    result = self.run_plan()
                self.assertIn(result['status'], ('skipped', 'paused'))
                self.assertEqual(self.js('return window.actions'), [])
                self.assert_unstaked()

    def test_expired_plan_and_expiry_during_claim_or_final_validation_never_submit(self):
        self.assertEqual(self.run_plan(plan_override={'deadline': '2000-01-01T00:00:00+00:00'})['status'], 'skipped')
        self.assert_unstaked()
        for stage in ('claim', 'confirm'):
            with self.subTest(stage=stage):
                self.reset()
                execute = self.driver.execute_script
                def expire():
                    execute('const now=Date.now();Date.now=()=>now+60000')
                def authorize(quote):
                    if stage == 'claim':
                        expire()
                    return {'stake': 2.61, 'min_decimal': 2.0}
                def dispatch(script, *args):
                    if stage == 'confirm' and script is execution.CONFIRM_PLAN_SCRIPT:
                        expire()
                    return execute(script, *args)
                with patch.object(self.driver, 'execute_script', side_effect=dispatch):
                    self.assertEqual(self.run_plan(authorize=authorize)['status'], 'skipped')
                self.assertEqual(self.js('return window.actions'), [])
                self.assert_unstaked()

    def test_signout_suspension_and_denied_claim_do_not_stage_money(self):
        for change in ("document.querySelector('section').setAttribute('aria-disabled','true')",
                       "document.querySelector('[data-testid=betslip-place-bet-button]').textContent='Sign In'"):
            with self.subTest(change=change):
                self.reset()
                self.js(change)
                self.assertIn(self.run_plan()['status'], ('skipped', 'paused'))
                self.assert_unstaked()
                self.assertEqual(self.js('return window.actions'), [])
        self.reset()
        self.assertEqual(self.run_plan(authorize=lambda q: {'denied': 'claim_denied'})['denied'], 'claim_denied')
        self.assert_unstaked()

    def test_explicit_accept_only_continuation_keeps_other_legs_unstaked(self):
        self.js("window.mode='accept-only';document.querySelector('[data-testid=betslip-place-bet-button]').textContent='Accept Odds Changes'")
        result = self.run_plan()
        self.assertEqual(result['status'], 'placed', result)
        actions = self.js('return window.actions')
        self.assertEqual(len(actions), 2)
        self.assertTrue(all(len(row)==1 and row[0]['tracking']['selectionId']=='s1' for row in actions))
        self.assert_unstaked()

    def test_post_click_changed_price_is_uncertain_and_never_rearmed_or_cleared(self):
        self.js("window.mode='price-change'")
        result = self.run_plan()
        self.assertEqual(result['status'], 'unconfirmed')
        self.assertIn('changed after claim', result['confirmationBlocked'])
        self.assertEqual(len(self.js('return window.actions')), 1)
        self.assertEqual(self.js("return document.querySelector('input').value"), '2.61')
        self.assertEqual(self.run_plan(2)['status'], 'stopped')
        self.assertEqual(len(self.js('return window.actions')), 1)

    def test_missing_receipt_never_replays_and_blocks_following_signal(self):
        self.js("window.mode='silent'")
        self.assertEqual(self.run_plan()['status'], 'unconfirmed')
        self.assertEqual(len(self.js('return window.actions')), 1)
        self.assertEqual(self.js("return document.querySelector('input').value"), '2.61')
        self.assertEqual(self.run_plan(2)['status'], 'stopped')

    def test_lost_driver_reply_after_real_click_has_no_retry_or_cleanup(self):
        self.js("window.mode='silent'")
        execute = self.driver.execute_script
        def dispatch(script, *args):
            result = execute(script, *args)
            if script is execution.CONFIRM_PLAN_SCRIPT:
                raise DriverConnectionLost('synthetic lost reply after real fixture click')
            return result
        with patch.object(self.driver, 'execute_script', side_effect=dispatch), self.assertRaises(DriverConnectionLost):
            self.run_plan()
        self.assertEqual(len(self.js('return window.actions')), 1)
        self.assertEqual(self.js("return document.querySelector('input').value"), '2.61')
        self.assertEqual(self.run_plan(2)['status'], 'stopped')

    def test_overlapping_h2h_suspended_a_yields_to_b_then_a_resumes_only_when_observed_ready(self):
        self.reset(h2h=True)
        self.js("document.querySelector('section').setAttribute('aria-disabled','true')")
        claim = Mock()
        start = time.monotonic()
        a = self.run_plan(0, authorize=claim, h2h=True, deadline_budget=.2)
        self.assertLess(time.monotonic()-start, .5)
        self.assertEqual(a['submissionState'], 'not-submitted')
        self.assertTrue(a['notReady'])
        self.assertTrue(a['retryable'])
        claim.assert_not_called()
        self.assert_unstaked()
        b = self.run_plan(1, h2h=True, deadline_budget=1)
        self.assertEqual(b['status'], 'placed', b)
        self.assertEqual(self.js('return window.actions')[0][0]['tracking']['eventId'], '11')
        self.assertFalse(execution.plan_slip_ready(self.driver, *h2h_contract(0)[:3]))
        self.js("document.querySelector('section').removeAttribute('aria-disabled')")
        self.assertTrue(execution.plan_slip_ready(self.driver, *h2h_contract(0)[:3]))
        self.assertEqual(self.run_plan(0, h2h=True)['status'], 'placed')
        self.assertEqual([row[0]['tracking']['eventId'] for row in self.js('return window.actions')], ['11', '10'])
        self.assert_unstaked()

    def test_overlapping_h2h_absent_a_yields_without_board_navigation_and_expired_a_is_not_replayed(self):
        self.reset(h2h=True)
        self.js("window.savedA=document.querySelector('section');window.savedA.remove()")
        start = time.monotonic()
        a = self.run_plan(0, h2h=True, deadline_budget=.2)
        self.assertLess(time.monotonic()-start, .5)
        self.assertTrue(a['retryable'])
        self.assertEqual(a['submissionState'], 'not-submitted')
        self.assertEqual(self.run_plan(1, h2h=True)['status'], 'placed')
        self.js("document.querySelector('[data-testid=betslip-content-wrapper]').prepend(window.savedA)")
        a = self.run_plan(0, h2h=True, plan_override={'deadline':'2000-01-01T00:00:00+00:00'})
        self.assertFalse(a['retryable'])
        self.assertEqual(a['submissionState'], 'not-submitted')
        self.assertEqual([row[0]['tracking']['eventId'] for row in self.js('return window.actions')], ['11'])
        self.assert_unstaked()

    def test_overlapping_h2h_unanswered_a_blocks_b_without_rearm_cleanup_or_replay(self):
        self.reset(h2h=True)
        self.js("window.mode='silent'")
        a = self.run_plan(0, h2h=True)
        self.assertEqual(a['submissionState'], 'uncertain')
        self.assertFalse(a['retryable'])
        writes = self.js('return window.inputEvents')
        b = self.run_plan(1, h2h=True)
        self.assertEqual(b['status'], 'stopped')
        self.assertEqual(b['submissionState'], 'uncertain')
        self.assertEqual(self.js('return window.inputEvents'), writes)
        self.assertEqual(len(self.js('return window.actions')), 1)

    def test_disabled_submit_wait_is_capped_and_cleans_a_before_b(self):
        self.js("document.querySelector('[data-testid=betslip-place-bet-button]').disabled=true")
        a = self.run_plan(deadline_budget=.5)
        self.assertTrue(a['retryable'], a)
        self.assertEqual(a['submissionState'], 'not-submitted')
        stages = [s for s in a['timing']['stages'] if s['name'] == 'planSlipArming']
        self.assertEqual(len(stages), 1)
        self.assertLess(stages[0]['durationMs'], 400)
        self.assert_unstaked()
        self.assertEqual(self.js('return window.actions'), [])
        self.js("document.querySelector('[data-testid=betslip-place-bet-button]').disabled=false")
        self.assertEqual(self.run_plan(2)['status'], 'placed')

    def test_dispatch_budget_expiring_during_claim_cannot_arm(self):
        def authorize(quote):
            time.sleep(.3)
            return {'stake':2.61, 'min_decimal':2.0}
        result = self.run_plan(authorize=authorize, deadline_budget=.25)
        self.assertEqual(result['submissionState'], 'not-submitted')
        self.assertFalse(result['retryable'])
        self.assertEqual(self.js('return window.inputEvents'), 0)
        self.assertEqual(self.js('return window.actions'), [])
        self.assert_unstaked()

    def test_plan_watcher_caps_ten_second_absent_wait_and_disconnects_without_money(self):
        self.js("document.querySelectorAll('section').forEach(e=>e.remove());"
                "document.querySelector('[data-testid=betslip-content-wrapper]').insertAdjacentHTML('afterbegin',"
                "'<div data-testid=betslip-empty-state-title>Empty</div>')")
        result = self.driver.execute_async_script(execution.WAIT_FOR_PLAN_ARM_SCRIPT,
            {**plan_slip_config(*contract()[:3]), 'readOnly':True, 'timeoutMs':10000, 'fallbackMs':50})
        self.assertEqual(result['status'], 'waiting')
        self.assertTrue(result['readiness']['timedOut'])
        self.assertLess(result['readiness']['waitMs'], 400)
        self.assertEqual(result['readiness']['stakeWrites'], 0)
        self.assertEqual(self.js('return window.inputEvents'), 0)

    def test_cleanup_follows_exact_replaced_card_after_definite_final_abort(self):
        execute = self.driver.execute_script
        def dispatch(script, *args):
            if script is execution.CONFIRM_PLAN_SCRIPT:
                execute("const card=document.querySelector('section');card.replaceWith(card.cloneNode(true));"
                        "document.querySelector('.line').textContent='-1.5'")
            return execute(script, *args)
        with patch.object(self.driver, 'execute_script', side_effect=dispatch):
            result = self.run_plan()
        self.assertEqual(result['submissionState'], 'not-submitted')
        self.assertEqual(result['status'], 'skipped')
        self.assert_unstaked()
        self.assertEqual(self.run_plan(2)['status'], 'placed')

    def test_new_non_target_money_at_confirm_is_not_cleared_or_submitted(self):
        execute = self.driver.execute_script
        def dispatch(script, *args):
            if script is execution.CONFIRM_PLAN_SCRIPT:
                execute("document.querySelectorAll('section input')[1].value='1.00'")
            return execute(script, *args)
        with patch.object(self.driver, 'execute_script', side_effect=dispatch):
            result = self.run_plan()
        self.assertEqual(result['status'], 'stopped')
        self.assertTrue(result['cleanupFailed'])
        self.assertEqual(self.js("return document.querySelector('input').value"), '')
        self.assertEqual(self.js("return document.querySelectorAll('section input')[1].value"), '1.00')
        self.assertEqual(self.js('return window.actions'), [])
        self.assertEqual(self.run_plan(2)['status'], 'stopped')

    def test_ambiguous_target_line_cannot_borrow_matching_number_elsewhere_on_card(self):
        self.js("document.querySelector('.line').textContent='-1.5';"
                "document.querySelector('section').insertAdjacentHTML('beforeend','<span>+1.5</span>')")
        claim = Mock()
        result = self.run_plan(authorize=claim)
        self.assertEqual(result['status'], 'skipped')
        claim.assert_not_called()
        self.assert_unstaked()

    def test_continuation_denial_or_stake_change_never_rearms_an_unresolved_submission(self):
        for values in ({'denied':'expired'}, {'stake':1.0,'min_decimal':2.0}):
            with self.subTest(values=values):
                self.reset()
                self.js("window.mode='accept-only';document.querySelector('[data-testid=betslip-place-bet-button]').textContent='Accept Odds Changes'")
                calls = []
                def authorize(quote):
                    calls.append(quote)
                    return {'stake':2.61,'min_decimal':2.0} if len(calls) == 1 else values
                result = self.run_plan(authorize=authorize)
                self.assertEqual(result['submissionState'], 'uncertain')
                self.assertEqual(self.js('return window.inputEvents'), 1)
                self.assertEqual(len(self.js('return window.actions')), 1)
                self.assertEqual(self.js("return document.querySelector('input').value"), '2.61')

    def test_missing_positive_authentication_evidence_never_writes_or_submits(self):
        self.js("document.getElementById('standardized-header-account-menu').remove();delete window.__isLoggedIn;delete window.__canBet;delete window.__playSpan")
        self.assertFalse(execution.plan_slip_ready(self.driver, *contract()[:3]))
        claim = Mock()
        result = self.run_plan(authorize=claim)
        self.assertEqual(result['submissionState'], 'not-submitted')
        claim.assert_not_called()
        self.assertEqual(self.js('return window.inputEvents'), 0)
        self.assertEqual(self.js('return window.actions'), [])

    def test_even_subcent_stake_mismatch_cannot_submit(self):
        config = plan_slip_config(*contract()[:3])
        armed = self.driver.execute_async_script(execution.WAIT_FOR_PLAN_ARM_SCRIPT,
            {**config,'stake':2.61,'timeoutMs':250,'fallbackMs':50})
        self.assertEqual(armed['status'], 'armed')
        self.js("document.querySelector('input').value='2.6101'")
        result = self.js(execution.CONFIRM_PLAN_SCRIPT, {**config,'stake':2.61,'minDecimal':2.0})
        self.assertEqual(result['status'], 'resize')
        self.assertEqual(self.js('return window.actions'), [])


    def test_exact_inventory_uses_real_tracking_ids_and_legacy_selection_list_stays_compatible(self):
        snapshot = self.js(execution.SLIP_LEGS_SCRIPT, {'exact': True})
        self.assertEqual([(leg['eventId'], leg['marketId'], leg['selectionId']) for leg in snapshot['legs']],
                         [('10', 'm2', 's1'), ('10', 'm3', 's2'), ('11', 'm2', 's3')])
        self.assertEqual(execution.read_slip_legs(self.driver), {'status':'present','legs':['s1','s2','s3']})
        self.assertEqual(self.js('return window.inputEvents'), 0)

    def test_nested_provider_odds_wrappers_are_one_price_but_separate_prices_are_ambiguous(self):
        self.js("const price=document.querySelector('[data-testid=betslip-odds-standard]');"
                "price.innerHTML='<span data-testid=betslip-odds-standard>+110</span>'")
        self.assertTrue(execution.plan_slip_ready(self.driver, *contract()[:3]))
        self.assertEqual(self.run_plan()['status'], 'placed')
        self.reset()
        self.js("document.querySelector('section').insertAdjacentHTML('beforeend',"
                "'<span data-testid=betslip-odds-standard>+110</span>')")
        claim = Mock()
        self.assertEqual(self.run_plan(authorize=claim)['status'], 'skipped')
        claim.assert_not_called()
        self.assertEqual(self.js('return window.actions'), [])

    def test_unknown_hidden_or_external_funded_input_never_claims_or_submits(self):
        for change in ("document.querySelector('[data-testid=betslip-content-wrapper]').insertAdjacentHTML('beforeend',"
                       "'<input type=hidden name=parlayStake value=1>')",
                       "document.body.insertAdjacentHTML('beforeend','<input name=stake value=1 style=display:none>')"):
            with self.subTest(change=change):
                self.reset()
                self.js(change)
                claim = Mock()
                self.assertEqual(self.run_plan(authorize=claim)['status'], 'skipped')
                claim.assert_not_called()
                self.assertEqual(self.js('return window.actions'), [])

    def test_other_leg_dom_values_and_selection_controls_are_preserved(self):
        before = self.js("return [...document.querySelectorAll('section')].slice(1).map(e=>e.outerHTML)")
        self.assertEqual(self.run_plan()['status'], 'placed')
        self.assertEqual(self.js("return [...document.querySelectorAll('section')].map(e=>e.outerHTML)"), before)
        self.assertEqual(self.js('return window.inputEvents'), 1)

    def test_ambiguous_replaced_target_cleanup_stops_without_clearing_other_legs(self):
        execute = self.driver.execute_script
        def dispatch(script, *args):
            if script is execution.CONFIRM_PLAN_SCRIPT:
                execute("const card=document.querySelector('section');card.replaceWith(card.cloneNode(true));"
                        "const fresh=document.querySelector('section');fresh.after(fresh.cloneNode(true))")
            return execute(script, *args)
        with patch.object(self.driver, 'execute_script', side_effect=dispatch):
            result = self.run_plan()
        self.assertEqual(result['status'], 'stopped')
        self.assertTrue(result['cleanupFailed'])
        self.assertEqual(self.js('return window.actions'), [])
        self.assertEqual(self.js("return [...document.querySelectorAll('section input')].map(e=>e.value)"),
                         ['2.61','2.61','',''])
        self.assertFalse(execution.plan_slip_ready(self.driver, *contract(2)[:3]))
        self.assertEqual(self.run_plan(2)['status'], 'stopped')

    def test_ancestor_suspension_duplicate_external_id_and_native_denial_override_ready_ui(self):
        for change in ("const card=document.querySelector('section');const wrapper=document.createElement('div');"
                       "card.replaceWith(wrapper);wrapper.appendChild(card);wrapper.setAttribute('aria-disabled','true')",
                       "document.querySelector('input').id='stake-id';document.body.insertAdjacentHTML('beforeend','<span id=stake-id></span>')",
                       "window.__isLoggedIn=false", "window.__canBet=false"):
            with self.subTest(change=change):
                self.reset()
                self.js(change)
                claim = Mock()
                self.assertFalse(execution.plan_slip_ready(self.driver, *contract()[:3]))
                result = self.run_plan(authorize=claim)
                self.assertIn(result['status'], ('skipped','paused'))
                claim.assert_not_called()
                self.assertEqual(self.js('return window.inputEvents'), 0)
                self.assertEqual(self.js('return window.actions'), [])

    def test_payout_recalculation_is_not_contract_drift_or_line_evidence(self):
        self.js("const card=document.querySelector('section');"
                "card.insertAdjacentHTML('beforeend','<div class=payout>Potential Payout $0.00</div>');"
                "card.querySelector('input').addEventListener('input',()=>card.querySelector('.payout').textContent='Potential Payout $5.48')")
        self.assertEqual(self.run_plan()['status'], 'placed')
        self.reset()
        self.js("document.querySelector('.line').textContent='';document.querySelector('section').insertAdjacentHTML("
                "'beforeend','<div>Potential Payout $1.50</div>')")
        claim = Mock()
        self.assertEqual(self.run_plan(authorize=claim)['status'], 'skipped')
        claim.assert_not_called()
        self.assert_unstaked()

    def test_hidden_zero_parlay_input_is_preserved_and_never_funded(self):
        self.js("document.querySelectorAll('input')[3].style.display='none'")
        self.assertTrue(execution.plan_slip_ready(self.driver, *contract()[:3]))
        self.assertEqual(self.run_plan()['status'], 'placed')
        self.assertEqual(self.js('return window.inputEvents'), 1)
        self.assert_unstaked()


if __name__ == '__main__':
    unittest.main()
