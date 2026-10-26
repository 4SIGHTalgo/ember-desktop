"""Inventory-backed, non-wager staging, including real native fixture clicks."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from selenium.webdriver.chrome.options import Options

from driver_lifecycle import create_owned_chrome
from ember_market_readiness import MarketReadiness
from ember_staging import Staging
import test_ember_scheduling as fixtures
import tt_market_exec as execution


def stage_hint(signal):
    event, market, plan = (signal['payload'][key] for key in ('event', 'market', 'plan'))
    now = datetime.now(timezone.utc)
    return {'protocol': 1, 'event_id': event['eventId'], 'event': event,
            'market_id': market['marketId'], 'market_name': market['name'], 'bet_type': plan['betType'],
            'selection_id': plan['selectionId'], 'label': plan['selection'], 'line': None,
            'updated_at': now.isoformat(), 'expires_at': (now+timedelta(seconds=10)).isoformat()}


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.stager = Staging()
        self.hints = [stage_hint(signal) for signal in fixtures.fixture_signals()]
        self.core = SimpleNamespace(click_selection=Mock(return_value={'status': 'clicked'}))
        self.driver = SimpleNamespace(_tt_plan_uncertain=False, _tt_plan_stopped=False)
        self.readiness = Mock()
        self.readiness.prepare.return_value = {'status': 'revealed'}
        self.snapshot = {'status': 'empty', 'stagingSafe': True, 'legs': []}
        reader = patch('ember_staging.read_plan_slip', return_value=self.snapshot)
        self.reader = reader.start()
        self.addCleanup(reader.stop)

    def step(self, hints=None, valid=True, focus=None, core=None):
        return self.stager.step(self.driver, core or self.core, self.readiness,
                                self.hints if hints is None else hints,
                                lambda: valid, time.time(), focus=focus)

    def test_one_native_selection_per_tick_and_round_robin_across_failed_events(self):
        self.readiness.prepare.return_value = {'status': 'not_ready'}
        self.step()
        self.step()
        self.step()
        self.step()
        self.assertEqual([call.args[1]['eventId'] for call in self.readiness.prepare.call_args_list], ['100', '100', '101', '101'])
        self.core.click_selection.assert_not_called()
        self.readiness.prepare.return_value = {'status': 'revealed'}
        self.assertEqual(self.step(), self.hints[0])
        self.core.click_selection.assert_called_once()
        self.assertLessEqual(self.core.click_selection.call_args.kwargs['timeout'], .3)

    def test_actual_inventory_prevents_toggle_even_if_target_arrives_during_reveal(self):
        def reveal(*args, **kwargs):
            self.snapshot['legs'].append({'eventId': '100', 'marketId': '200', 'selectionId': '100a'})
            return {'status': 'revealed'}
        self.readiness.prepare.side_effect = reveal
        self.step()
        self.core.click_selection.assert_not_called()

    def test_navigation_spending_a_whole_turn_can_finish_on_its_next_tick(self):
        now = [0.0]
        def slow_navigation(*args, **kwargs):
            now[0] += .3
            return {'status': 'not_ready'}
        self.readiness.prepare.side_effect = slow_navigation
        with patch('ember_staging.time.monotonic', side_effect=lambda: now[0]):
            self.assertIsNone(self.step())
            self.core.click_selection.assert_not_called()
            self.readiness.prepare.side_effect = None
            self.assertEqual(self.step(), self.hints[0])
            self.assertEqual(self.step(), self.hints[1])
        self.assertEqual(self.core.click_selection.call_count, 2)

    def test_positive_reveal_at_slice_end_gets_one_native_click_without_another_wait(self):
        now = [0.0]
        def reveal(*args, **kwargs):
            now[0] += .3
            return {'status': 'revealed'}
        self.readiness.prepare.side_effect = reveal
        with patch('ember_staging.time.monotonic', side_effect=lambda: now[0]):
            self.assertEqual(self.step(), self.hints[0])
        self.core.click_selection.assert_called_once()
        self.assertLessEqual(self.core.click_selection.call_args.kwargs['timeout'], .001)

    def test_focus_limits_staging_to_the_arbiters_page_and_never_navigates(self):
        core = SimpleNamespace(click_selection=Mock(return_value={'status': 'clicked'}),
                               correct_event=Mock(return_value=True))
        self.assertEqual(self.step(core=core, focus="100"), self.hints[0])
        self.assertEqual([call.args[1] for call in self.readiness.prepare.call_args_list],
                         [self.hints[0]['event']])
        core.click_selection.assert_called_once()
        self.assertEqual(self.step(core=core, focus="101"), self.hints[1])
        self.assertEqual(core.click_selection.call_count, 2)
        core.correct_event.return_value = False
        self.readiness.prepare.reset_mock()
        core.click_selection.reset_mock()
        self.assertIsNone(self.step(core=core, focus="101"))
        self.readiness.prepare.assert_not_called()
        core.click_selection.assert_not_called()

    def test_recent_slip_read_is_reusable_only_within_the_same_tick(self):
        self.step()
        self.assertIsNotNone(self.stager.recent_snapshot())
        self.assertIsNone(self.stager.recent_snapshot(max_age=-1))

    def test_expiry_disconnect_funded_slip_and_uncertainty_never_click(self):
        expired = {**self.hints[0], 'expires_at': (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()}
        self.step([expired])
        self.step(valid=False)
        self.snapshot['stagingSafe'] = False
        self.step()
        self.snapshot['stagingSafe'] = True
        self.driver._tt_plan_uncertain = True
        self.step()
        self.core.click_selection.assert_not_called()
        self.readiness.prepare.assert_not_called()

    def test_same_market_hints_supply_all_absent_outcomes_and_full_event_identity(self):
        other = {**self.hints[0], 'selection_id': '100b', 'label': 'One'}
        self.step([self.hints[0], other])
        args = self.readiness.prepare.call_args.args
        self.assertEqual(args[1], self.hints[0]['event'])
        self.assertEqual(args[2]['eventId'], '100')
        self.assertEqual({row['selectionId'] for row in args[2]['outcomes']}, {'100a', '100b'})

    def test_pre_window_moneyline_reveal_requires_explicit_staging_and_keeps_exact_identity(self):
        signal = fixtures.fixture_signals()[0]
        event, market = (signal['payload'][key] for key in ('event', 'market'))
        market.update(name='Moneyline', scoreState=[1, 0])
        driver = Mock()
        driver.current_url = 'http://127.0.0.1/event/fixture/100'
        driver.execute_script.return_value = {'status': 'revealed'}
        with patch.object(execution, 'correct_event', return_value=True), patch.object(execution, 'ensure_market_tab', return_value=True):
            for staging in (False, None, 'true'):
                self.assertEqual(execution.reveal_set_winner(driver, event, market, staging=staging)['status'], 'wrong event')
            driver.execute_script.assert_not_called()
            self.assertEqual(execution.reveal_set_winner(driver, event, market, staging=True)['status'], 'revealed')
        driver.execute_script.assert_called_once()
        script, config = driver.execute_script.call_args.args
        self.assertEqual(script, execution.REVEAL_SCRIPT)
        self.assertEqual(config['players'], ['One', 'Two'])
        self.assertEqual(config['testIds'], ['component-builder-market-button-100-100a', 'component-builder-market-button-100-100b'])


class StagingBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        resources = ExitStack()
        cls.addClassCleanup(resources.close)
        cls.signals = fixtures.fixture_signals()
        cls.server, _ = resources.enter_context(fixtures.scheduling_server(cls.signals))
        options = Options()
        options.add_argument('--headless=new')
        options.add_argument('--window-size=1400,1000')
        cls.driver = create_owned_chrome(options)
        cls.addClassCleanup(cls.driver.quit)

    def test_all_matches_stage_natively_at_zero_stake_and_survive_hint_expiry(self):
        driver = self.driver
        driver.get(f'http://127.0.0.1:{self.server.server_port}/event/fixture/100')
        driver.execute_script('document.querySelectorAll("section").forEach(e=>e.remove());window.renderRows(0)')
        hints = fixtures.fixture_stages(self.signals)
        stager, readiness = Staging(), MarketReadiness(execution)
        with patch.object(execution, 'correct_event', side_effect=fixtures.local_identity):
            for _ in range(12):
                stager.step(driver, execution, readiness, hints, lambda: True, time.time())
                if len(execution.read_plan_slip(driver)['legs']) == 4:
                    break
            snapshot = execution.read_plan_slip(driver)
            self.assertEqual({leg['eventId'] for leg in snapshot['legs']}, {'100', '101'})
            self.assertEqual(len(snapshot['legs']), 4)
            self.assertTrue(snapshot['stagingSafe'])
            for _ in range(2):
                stager.step(driver, execution, readiness, [], lambda: True, time.time()+30)
            self.assertEqual(len(execution.read_plan_slip(driver)['legs']), 4)
        clicks = driver.execute_script('return window.selectionClicks')
        self.assertEqual(len(clicks), 4)
        self.assertTrue(all(click['trusted'] for click in clicks))
        self.assertEqual(driver.execute_script('return window.inputEvents'), 0)
        self.assertEqual(driver.execute_script('return window.actions'), [])

    def test_moneyline_stages_both_sides_before_closeout_without_weakening_execution_reveal(self):
        signals = fixtures.fixture_signals(h2h=True)
        with fixtures.scheduling_server(signals) as (server, _):
            driver = self.driver
            driver.get(f'http://127.0.0.1:{server.server_port}/event/fixture/100')
            driver.execute_script('document.querySelectorAll("section").forEach(e=>e.remove());window.renderRows(0)')
            hints = fixtures.fixture_stages(signals)[:2]
            for hint in hints:
                event = deepcopy(hint['event'])
                rows = event['sets'][:1]+[{'set': 2, 'home': 3, 'away': 2}]
                event.update(sets=rows, currentSet=rows[-1], setTally=[1, 0], period='2nd')
                hint['event'] = event
            market = {**signals[0]['payload']['market'], 'scoreState': [1, 0]}
            stager, readiness = Staging(), MarketReadiness(execution)
            with patch.object(execution, 'correct_event', side_effect=fixtures.local_identity):
                self.assertEqual(execution.reveal_set_winner(driver, hints[0]['event'], market)['status'], 'wrong event')
                for _ in range(8):
                    stager.step(driver, execution, readiness, hints, lambda: True, time.time())
                    if len(execution.read_plan_slip(driver)['legs']) == 2:
                        break
                self.assertEqual(execution.reveal_set_winner(driver, hints[0]['event'], market)['status'], 'wrong event')
            snapshot = execution.read_plan_slip(driver)
            self.assertEqual({leg['selectionId'] for leg in snapshot['legs']}, {'0ML100a', '0ML100b'})
            self.assertTrue(snapshot['stagingSafe'])
            self.assertEqual(len(driver.execute_script('return window.selectionClicks')), 2)
            self.assertTrue(all(row['trusted'] for row in driver.execute_script('return window.selectionClicks')))
            self.assertEqual(driver.execute_script('return window.inputEvents'), 0)
            self.assertEqual(driver.execute_script('return window.actions'), [])


if __name__ == '__main__':
    unittest.main()
