from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import ExitStack
import os
import queue
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from ember_client import Client, Ledger, SignalQueue


def plan_signal(**plan_overrides):
    from test_tt_market_semantics import state_event
    now = datetime.now(timezone.utc)
    plan = {"betType": "split", "selection": "Two", "selectionId": "300", "scoreState": [1, 0], "nextSet": 2,
            "specification": {"family": "split", "player": 1, "target": 2}, "min_decimal": 1.85,
            "stake_cents": 261, "deadline": (now+timedelta(seconds=4)).isoformat(),
            "display": {"detail": "Server plan"}}
    return {"protocol": 2, "id": 1, "contract": "100:split", "expires_at": (now+timedelta(seconds=5)).isoformat(),
            "payload": {"event": state_event("1", eventId="100", player1="One", player2="Two"),
                        "market": {"eventId": "100", "marketId": "200", "name": "Game 2",
                                   "outcomes": [{"selectionId": "300", "decimal": 2.0, "label": "Two", "points": None},
                                                {"selectionId": "301", "decimal": 2.0, "label": "One", "points": None}]},
                        "plan": {**plan, **plan_overrides}}}
from ember_protocol import RunLease, validate_preparation, validate_scanner, validate_signal, validate_stage


class ClientProtocolTests(unittest.TestCase):
    def test_waiting_quote_is_replaced_without_losing_other_contracts_fifo_turns(self):
        waiting = SignalQueue(maxsize=2)
        waiting.put_latest({'id': 1, 'contract': 'one', 'price': 2})
        waiting.put_latest({'id': 2, 'contract': 'two'})
        waiting.put_latest({'id': 3, 'contract': 'one', 'price': 3})
        waiting.put_latest({'id': 1, 'contract': 'one', 'price': 1})
        self.assertEqual(waiting.qsize(), 2)
        self.assertEqual(waiting.get_nowait(), {'id': 3, 'contract': 'one', 'price': 3})
        self.assertEqual(waiting.get_nowait()['contract'], 'two')

    def test_readiness_retry_goes_behind_other_contracts_and_cannot_replace_a_newer_quote(self):
        waiting = SignalQueue(maxsize=2)
        first = {'id': 1, 'contract': 'one'}
        waiting.put_latest(first)
        waiting.put_latest({'id': 2, 'contract': 'two'})
        self.assertEqual(waiting.get_nowait(), first)
        waiting.put_latest(first)
        self.assertEqual(waiting.get_nowait()['contract'], 'two')
        waiting.put_latest({'id': 3, 'contract': 'one'})
        waiting.put_latest(first)
        self.assertEqual(waiting.get_nowait()['id'], 3)

    def test_full_queue_still_accepts_replacements_but_rejects_new_contracts(self):
        waiting = SignalQueue(maxsize=1)
        waiting.put_latest({'id': 1, 'contract': 'one'})
        waiting.put_latest({'id': 2, 'contract': 'one'})
        with self.assertRaises(queue.Full):
            waiting.put_latest({'id': 3, 'contract': 'two'})

    def test_preparation_can_navigate_but_cannot_carry_wager_commands(self):
        preparation = {"protocol": 1, "event": {"eventId": "100", "player1": "One", "player2": "Two"},
                       "market_hint": {"betType": "closeout"},
                       "expires_at": (datetime.now(timezone.utc)+timedelta(seconds=15)).isoformat()}
        validate_preparation(preparation)
        preparation["market_hint"]["submit"] = True
        with self.assertRaises(ValueError): validate_preparation(preparation)

    def test_v1_strategy_signals_are_rejected_fail_closed(self):
        from test_tt_market_semantics import state_event
        signal = {"protocol": 1, "id": 1, "contract": "100:split",
                  "expires_at": (datetime.now(timezone.utc)+timedelta(seconds=5)).isoformat(),
                  "payload": {"event": state_event("1", eventId="100", player1="One", player2="Two"),
                              "market": {"eventId": "100", "marketId": "200", "name": "Game 2"},
                              "outcome": {"selectionId": "300", "decimal": 2.0, "label": "Two"},
                              "context": {"betType": "split", "selection": "Two"},
                              "settings": {"ev": 0, "confidence": 90, "cost": 0, "ev_mode": "conservative"}}}
        with self.assertRaises(ValueError):
            validate_signal(signal)

    @staticmethod
    def stage(**overrides):
        now = datetime.now(timezone.utc)
        return {"protocol": 1, "event_id": "100", "bet_type": "split",
                "market_name": "Game 2", "market_id": "200", "selection_id": "s2",
                "label": "Tomasz Lojtek", "line": None,
                "updated_at": now.isoformat(), "expires_at": (now+timedelta(seconds=5)).isoformat(),
                **overrides}

    def test_pre_open_hint_is_identity_only_and_strictly_bounded(self):
        hint = self.stage()
        self.assertIs(validate_stage(hint), hint)
        for field, value in (("protocol", 2), ("bet_type", "correct_score"), ("line", "1.5"),
                             ("market_id", "x"*65), ("label", ""), ("market_name", "")):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    validate_stage({**hint, field: value})
        with self.subTest(case="extra field"):
            with self.assertRaises(ValueError):
                validate_stage({**hint, "settings": {"ev": 0}})
        with self.subTest(case="nonpositive lifetime"):
            with self.assertRaises(ValueError):
                validate_stage({**hint, "expires_at": hint["updated_at"]})

    def test_scanner_relay_carries_or_drops_the_pre_open_hint(self):
        scanner = {"healthy": True, "counts": {}, "stage": self.stage()}
        self.assertEqual(validate_scanner(scanner)["stage"], scanner["stage"])
        malformed = {**scanner, "stage": {"protocol": 1}}
        self.assertIsNone(validate_scanner(malformed)["stage"])
        # Older servers never published the hint; it stays absent, never fatal.
        self.assertIsNone(validate_scanner({"healthy": True, "counts": {}})["stage"])
        self.assertIsNone(validate_stage(None))

    def test_lease_stops_on_disconnect_expiry_and_stop(self):
        now = datetime.now(timezone.utc)
        clock = [0]
        lease = RunLease(clock=lambda: clock[0]); lease.running = True
        lease.update({"enabled": True, "client_supported": True, "server_time": now.isoformat(),
                      "access_until": (now+timedelta(hours=1)).isoformat(), "lease_seconds": 5})
        signal = {"expires_at": (now+timedelta(seconds=5)).isoformat()}
        self.assertTrue(lease.permits(signal))
        clock[0] = 6
        self.assertFalse(lease.permits(signal))
        clock[0] = 0; lease.revoke()
        self.assertFalse(lease.permits(signal))

    def test_execution_reservation_survives_restart_and_is_per_account(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"ledger.sqlite3"
            first = Ledger(path)
            self.assertTrue(first.reserve("one", "100:split"))
            first.connection.close()
            second = Ledger(path)
            self.assertFalse(second.reserve("one", "100:split"))
            self.assertTrue(second.reserve("two", "100:split"))
            second.connection.close()


class ProtocolV2Tests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": directory.name,
            "EMBER_DEVICE_TEST_ONCE": "", "EMBER_DEVICE_TEST_LOGIN_ONLY": "", "EMBER_DEVICE_TEST_SCAN_ONLY": "",
            "EMBER_DEVICE_BROWSER_TICKET": ""}))
        self.stack.enter_context(patch.dict(sys.modules, {"keyring": Mock()}))
        self.client = Client()
        self.client.api = "https://example.com"
        self.client.account = "test-account"
        self.client.lease.running = True
        now = datetime.now(timezone.utc)
        self.client.lease.update({"enabled": True, "client_supported": True, "server_time": now.isoformat(),
                                  "access_until": (now+timedelta(hours=1)).isoformat(), "lease_seconds": 5})

    plan_signal = staticmethod(plan_signal)

    def test_plan_signal_is_accepted_and_cannot_carry_strategy_data(self):
        signal = self.plan_signal()
        self.assertIs(validate_signal(signal), signal)
        for field, value in (("protocol", 3), ("contract", "100:game-3")):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    validate_signal({**signal, field: value})
        with self.subTest(case="payload pollution"):
            with self.assertRaises(ValueError):
                validate_signal({**signal, "payload": {**signal["payload"], "settings": {"ev": 0}}})
        with self.subTest(case="strategy data in the plan"):
            with self.assertRaises(ValueError):
                validate_signal(self.plan_signal(conditioningCount=30, successCount=25))

    def test_plan_rejects_invalid_floors_stakes_and_deadlines(self):
        cases = [("zero stake", {"stake_cents": 0}), ("fractional stake", {"stake_cents": 261.5}),
                 ("oversized stake", {"stake_cents": 10_000_001}), ("non-int stake", {"stake_cents": "261"}),
                 ("no price floor", {"min_decimal": 1.0}), ("unreadable floor", {"min_decimal": float("inf")}),
                 ("excluded family", {"betType": "correct_score"}), ("empty selection id", {"selectionId": ""}),
                 ("empty selection", {"selection": ""}),
                 ("spec without player", {"specification": {"family": "split", "target": 2}}),
                 ("spec family mismatch", {"specification": {"family": "closeout", "player": 1, "target": 2}}),
                 ("unknown spec key", {"specification": {"family": "split", "player": 1, "target": 2, "edge": 0.9}}),
                 ("target out of range", {"nextSet": 6}),
                 ("deadline past expiry", {"deadline": (datetime.now(timezone.utc)+timedelta(seconds=30)).isoformat()}),
                 ("display dict value", {"display": {"detail": {"nested": True}}})]
        for name, overrides in cases:
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    validate_signal(self.plan_signal(**overrides))

    def test_plan_must_match_its_market_surface_and_entry_window(self):
        from test_tt_market_semantics import state_event
        def closed_entry(signal):
            signal["payload"]["event"] = state_event("1", eventId="100", player1="One", player2="Two", points=(3, 0))
        mutations = {"wrong event": lambda s: s["payload"]["market"].update(eventId="999"),
                     "unsupported market": lambda s: s["payload"]["market"].update(name="Correct Score"),
                     "market mismatch": lambda s: s["payload"]["market"].update(name="Game 3"),
                     "renamed selection": lambda s: s["payload"]["plan"].update(selection="One"),
                     "missing outcomes": lambda s: s["payload"]["market"].update(outcomes=[]),
                     "ambiguous outcomes": lambda s: s["payload"]["market"]["outcomes"].append(
                         dict(s["payload"]["market"]["outcomes"][0])),
                     "wrong score state": lambda s: s["payload"]["plan"].update(scoreState=[0, 1]),
                     "closed entry window": closed_entry}
        for name, mutate in mutations.items():
            with self.subTest(case=name):
                signal = self.plan_signal()
                mutate(signal)
                with self.assertRaises(ValueError):
                    validate_signal(signal)

    def test_v2_policy_is_the_server_plan_not_a_client_ev_gate(self):
        # The stream no longer gates on local strategy; the validated plan is
        # the decision and validate_signal already rejected everything else.
        import ember_client
        self.assertFalse(hasattr(ember_client.Client, "policy_passes"))

    def test_v2_plans_reserve_the_same_economic_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory)/"execution.sqlite3")
            signal = self.plan_signal()
            self.assertTrue(ledger.reserve_signal("one", signal))
            self.assertFalse(ledger.reserve_signal("one", signal))
            self.assertTrue(ledger.reserve_signal("two", signal))
            ledger.connection.close()

    def test_plan_signal_executes_by_comparison_and_journals_the_outcome(self):
        import tt_market_exec as core
        client = self.client
        client.provider_ready.set()
        client.lease.running = True
        signal = self.plan_signal()
        client.receive_opportunity(signal, client.history_scope())

        deliveries = iter([signal])

        def deliver(**_):
            try:
                return next(deliveries)
            except StopIteration:
                client.lease.running = False
                raise queue.Empty

        client.authorize = Mock(return_value=True)
        client.signals = Mock()
        client.signals.get.side_effect = deliver
        client.signals.get_nowait.side_effect = queue.Empty
        driver = Mock(current_url=core.LEAGUE_URL)
        owner = Mock(retained=False)
        owner.open.return_value = driver
        owner.recovery.return_value = "closed"
        gate = Mock()
        gate.poll.return_value = {"ready": True, "signed_in": True, "signed_out_confirmed": False,
                                  "provider_can_bet": True, "native_wagering_grant": True,
                                  "location": {"state": "unknown", "blocked": False}, "message": None}
        with ExitStack() as stack:
            for setting in (patch("ember_client.emit"), patch("ember_client.validation_ticket", return_value=None),
                patch("ember_client.Ledger"), patch("ember_client.OwnedBrowser", return_value=owner),
                patch("ember_client.ActiveSessionPower"),
                patch.object(client.staging, 'add', return_value={'status': 'selected'}),
                patch("draftkings_login.allow_location_permissions"),
                patch("draftkings_login.goto_tt_elite"), patch("ember_provider_login.ProviderLoginGate", return_value=gate),
                patch("draftkings_session.SessionMaintenance"), patch.object(core, "prepare_browser_execution"),
                patch.object(core, "reveal_set_winner", return_value={"status": "revealed"}),
                patch.object(core, "open_props", return_value=True),
                patch.object(core, "ROOT", client.data_dir), patch.object(core, "SLIP_CAPTURE", client.data_dir/"slip.json")):
                stack.enter_context(setting)
            placed = {"status": "placed", "submitted": True, "stake": 2.61, "receiptPlaced": True,
                      "comparison": {"slipDecimal": 2.5, "estimatedNetEVPercent": None}}
            plan_exec = stack.enter_context(patch.object(core, "place_plan", return_value=placed))
            client.execute()
        plan_exec.assert_called_once()
        args, kwargs = plan_exec.call_args
        self.assertEqual(args[3], signal["payload"]["plan"])
        self.assertEqual(args[4]["selectionId"], "300")
        self.assertTrue(callable(kwargs["authorize_values"]))
        self.assertTrue(callable(kwargs["validate"]))
        snapshot = client.history.snapshot(client.history_scope())
        self.assertEqual(snapshot["counts"], {"placed": 1})
        self.assertEqual(snapshot["records"][0]["reason"], "placed")

    def test_plan_denials_are_journaled_with_the_authoritative_reason(self):
        import tt_market_exec as core
        client = self.client
        client.provider_ready.set()
        client.lease.running = True
        signal = self.plan_signal()
        client.receive_opportunity(signal, client.history_scope())
        deliveries = iter([signal])

        def deliver(**_):
            try:
                return next(deliveries)
            except StopIteration:
                client.lease.running = False
                raise queue.Empty

        client.authorize = Mock(return_value=True)
        client.signals = Mock()
        client.signals.get.side_effect = deliver
        client.signals.get_nowait.side_effect = queue.Empty
        driver = Mock(current_url=core.LEAGUE_URL)
        owner = Mock(retained=False)
        owner.open.return_value = driver
        owner.recovery.return_value = "closed"
        gate = Mock()
        gate.poll.return_value = {"ready": True, "signed_in": True, "signed_out_confirmed": False,
                                  "provider_can_bet": True, "native_wagering_grant": True,
                                  "location": {"state": "unknown", "blocked": False}, "message": None}
        withheld = {"status": "skipped", "submitted": False, "denied": "claimed_elsewhere"}
        with ExitStack() as stack:
            for setting in (patch("ember_client.emit"), patch("ember_client.validation_ticket", return_value=None),
                patch("ember_client.Ledger"), patch("ember_client.OwnedBrowser", return_value=owner),
                patch("ember_client.ActiveSessionPower"),
                patch.object(client.staging, 'add', return_value={'status': 'selected'}),
                patch("draftkings_login.allow_location_permissions"),
                patch("draftkings_login.goto_tt_elite"), patch("ember_provider_login.ProviderLoginGate", return_value=gate),
                patch("draftkings_session.SessionMaintenance"), patch.object(core, "prepare_browser_execution"),
                patch.object(core, "reveal_set_winner", return_value={"status": "revealed"}),
                patch.object(core, "open_props", return_value=True),
                patch.object(core, "ROOT", client.data_dir), patch.object(core, "SLIP_CAPTURE", client.data_dir/"slip.json")):
                stack.enter_context(setting)
            stack.enter_context(patch.object(core, "place_plan", return_value=withheld))
            client.execute()
        snapshot = client.history.snapshot(client.history_scope())
        self.assertEqual(snapshot["counts"], {"handled": 1})
        self.assertEqual(snapshot["records"][0]["reason"], "claimed_elsewhere")

    def test_claim_values_only_accepts_a_server_sized_plan(self):
        client = self.client
        client.provider_ready.set()
        client.lease.running = True
        now = datetime.now(timezone.utc)
        lease = {"enabled": True, "client_supported": True, "server_time": now.isoformat(),
                 "access_until": (now+timedelta(hours=1)).isoformat(), "lease_seconds": 5}
        claim = client.claim_values(self.plan_signal())
        quote = {"balance": {"availableBalance": 100.0}, "decimalOdds": 2.5}
        with patch.object(client, "request",
                          return_value={**lease, "plan": {"stake_cents": 261, "min_decimal": 1.85, "echo": "e1"}}):
            self.assertEqual(claim(quote), {"stake": 2.61, "min_decimal": 1.85, "echo": "e1"})
        cases = {"missing echo": {"stake_cents": 261, "min_decimal": 1.85},
                 "zero stake": {"stake_cents": 0, "min_decimal": 1.85, "echo": "e1"},
                 "no floor": {"stake_cents": 261, "min_decimal": 1, "echo": "e1"},
                 "extra field": {"stake_cents": 261, "min_decimal": 1.85, "echo": "e1", "probability": .6}}
        for name, plan in cases.items():
            with self.subTest(case=name):
                with patch.object(client, "request", return_value={**lease, "plan": plan}):
                    self.assertEqual(claim(quote), {"denied": "authorization_denied"})
        with self.subTest(case="no plan"):
            with patch.object(client, "request", return_value=dict(lease)):
                self.assertEqual(claim(quote), {"denied": "authorization_denied"})
        with self.subTest(case="a stricter claim floor is the authority"):
            with patch.object(client, "request",
                              return_value={**lease, "plan": {"stake_cents": 130, "min_decimal": 1.9, "echo": "e2"}}):
                self.assertEqual(claim(quote), {"stake": 1.30, "min_decimal": 1.9, "echo": "e2"})
        with self.subTest(case="claimed elsewhere"):
            with patch.object(client, "request", side_effect=RuntimeError("contract_claimed_by_another_client")):
                self.assertEqual(claim(quote), {"denied": "claimed_elsewhere"})
        with self.subTest(case="server floor refusal"):
            with patch.object(client, "request", side_effect=RuntimeError("price_below_floor")):
                self.assertEqual(claim(quote), {"denied": "odds_below_ev"})
        with self.subTest(case="unreadable quote"):
            self.assertEqual(claim({"balance": {}, "decimalOdds": None}), {"denied": "sizing_failed"})


class ProviderStartupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": directory.name,
            "EMBER_DEVICE_TEST_ONCE": "", "EMBER_DEVICE_TEST_LOGIN_ONLY": "", "EMBER_DEVICE_TEST_SCAN_ONLY": "", "EMBER_DEVICE_BROWSER_TICKET": ""}))
        self.stack.enter_context(patch.dict(sys.modules, {"keyring": Mock()}))
        self.client = Client()
        self.client.api = "https://example.com"
        self.client.account = "test-account"
        self.client.lease.running = True
        now = datetime.now(timezone.utc)
        self.client.lease.update({"enabled": True, "client_supported": True, "server_time": now.isoformat(),
                                  "access_until": (now+timedelta(hours=1)).isoformat(), "lease_seconds": 5})

    @staticmethod
    def state(ready, signed_in=False, signed_out=False):
        return {"ready": ready, "signed_in": signed_in, "signed_out_confirmed": signed_out,
                "provider_can_bet": ready, "native_wagering_grant": ready,
                "location": {"state": "unknown", "blocked": False}, "message": None}

    def test_signal_authorization_requires_provider_readiness(self):
        signal = {"expires_at": (datetime.now(timezone.utc)+timedelta(seconds=5)).isoformat()}
        self.assertFalse(self.client.permits(signal))
        self.client.provider_ready.set()
        self.assertTrue(self.client.permits(signal))
        self.client.readiness_only = True
        self.assertFalse(self.client.permits(signal))
        self.client.readiness_only = False
        self.client.run_cancel.set()
        self.assertFalse(self.client.permits(signal))
        self.client.run_cancel.clear()
        self.client.stop.set()
        self.assertFalse(self.client.permits(signal))

    def test_contract_rejection_does_not_revoke_other_signals_healthy_lease(self):
        self.client.provider_ready.set()
        signal = plan_signal()
        for error, reason in (('contract_claimed_by_another_client', 'claimed_elsewhere'),
                              ('signal_expired', 'signal_expired')):
            with self.subTest(error=error):
                self.client.request = Mock(side_effect=RuntimeError(error))
                until = self.client.lease.until
                self.assertFalse(self.client.authorize(signal))
                self.assertEqual(self.client.last_authorize_reason, reason)
                self.assertEqual(self.client.lease.until, until)
                self.assertTrue(self.client.permits(signal))
        self.client.request = Mock(return_value={
            'enabled': True, 'client_supported': True, 'lease_seconds': 5,
            'server_time': datetime.now(timezone.utc).isoformat(),
            'access_until': (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()})
        self.assertTrue(self.client.authorize(signal, claim=True))

    def test_global_authorization_error_still_revokes_the_run_lease(self):
        self.client.provider_ready.set()
        self.client.request = Mock(side_effect=RuntimeError('subscription_or_update_required'))
        self.assertFalse(self.client.authorize(plan_signal()))
        self.assertEqual(self.client.lease.until, 0)

    def test_expired_signal_cannot_be_requeued_for_readiness(self):
        self.client.provider_ready.set()
        signal = plan_signal()
        signal['expires_at'] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        self.assertFalse(self.client.defer_readiness(signal, {'status': 'not_ready'}))
        self.assertTrue(self.client.signals.empty())

    def test_unexpected_browser_close_alerts_once_and_revokes_execution(self):
        self.client.provider_ready.set()
        with patch("ember_client.emit") as emit:
            self.assertTrue(self.client.browser_closed())
            self.assertFalse(self.client.browser_closed())
        self.assertFalse(self.client.provider_ready.is_set())
        self.assertEqual(self.client.lease.until, 0)
        alerts = [call for call in emit.call_args_list if call.args == ("alert",)]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].kwargs["code"], "browser_closed")

    def test_intentional_stop_and_app_exit_do_not_sound_a_browser_alert(self):
        with patch("ember_client.emit") as emit:
            self.client.lease.running = False
            self.assertFalse(self.client.browser_closed())
            self.client.lease.running = True
            self.client.stop.set()
            self.assertFalse(self.client.browser_closed())
        emit.assert_not_called()

    def test_update_before_ember_login_never_attempts_a_relative_http_request(self):
        self.client.api = ""
        with patch("ember_client.requests.Session") as session:
            with self.assertRaisesRegex(RuntimeError, "sign_in_required"):
                self.client.ensure_access()
        session.assert_not_called()

    def run_startup(self, states, readiness_only, scan_only=False):
        import tt_market_exec as core
        from ember_device_test import DeviceTest
        client = self.client
        client.readiness_only = readiness_only
        client.scan_only = scan_only
        if readiness_only:
            client.device_test = DeviceTest(client.data_dir/"device-tests", "test", readiness_only=True, scan_only=scan_only)
            client.device_test.begin(1)
            self.assertFalse(client.device_test.claim({"id": 1, "payload": {"event": {"eventId": "1"}}}))
        driver = Mock(current_url="about:blank")
        driver.execute_script.return_value = {}
        owner = Mock(retained=False)
        owner.open.return_value = driver
        owner.recovery.return_value = "closed"
        events, incoming = [], iter(states)
        gate = Mock()

        def poll(*_):
            try:
                state = next(incoming)
            except StopIteration:
                client.lease.running = False
                state = self.state(False)
            if isinstance(state, BaseException):
                raise state
            events.append(("provider", state["ready"]))
            return state

        gate.poll.side_effect = poll
        client.stop.wait = Mock(side_effect=lambda _: setattr(client.lease, "running", False)
            if readiness_only and client.device_test.state.get("phase") in {"login_ready_no_wagers", "watching_verified_event_no_wagers", "league_standby_no_wagers"} else None)

        def no_signal(**_):
            events.append(("signal_wait", client.provider_ready.is_set()))
            client.lease.running = False
            raise queue.Empty

        client.signals = Mock()
        client.signals.get.side_effect = no_signal
        client.signals.get_nowait.side_effect = queue.Empty
        client.authorize = Mock(return_value=True)
        def open_league(_):
            driver.current_url = core.LEAGUE_URL
            events.append(("table_tennis", True))
        with ExitStack() as stack:
            for setting in (patch("ember_client.emit"),
                patch("ember_client.validation_ticket", return_value=None),
                patch("ember_client.OwnedBrowser", return_value=owner),
                patch("ember_client.ActiveSessionPower"),
                patch("ember_client.Ledger"),
                patch("draftkings_login.allow_location_permissions"),
                patch("draftkings_login.goto_tt_elite", side_effect=open_league),
                patch("ember_provider_login.ProviderLoginGate", return_value=gate),
                patch("ember_market_readiness.MarketReadiness.warm", return_value={"status": "ready"}),
                patch("draftkings_session.SessionMaintenance"),
                patch.object(core, "prepare_browser_execution"),
                patch.object(core, "ROOT", client.data_dir),
                patch.object(core, "SLIP_CAPTURE", client.data_dir/"last-slip.json")):
                stack.enter_context(setting)
            plan_exec = stack.enter_context(patch.object(core, "place_plan"))
            props = stack.enter_context(patch.object(core, "open_props", return_value=True))
            if scan_only:
                gate.navigation_started.side_effect = lambda: events.append(("navigation_grace", True))
            client.execute()
            plan_exec.assert_not_called()
            if not scan_only:
                props.assert_not_called()
            else:
                props.assert_called_once()
        client.authorize.assert_not_called()
        return driver, events

    def test_signed_out_start_opens_login_and_never_table_tennis(self):
        from draftkings_login import LOGIN_URL
        driver, events = self.run_startup([self.state(False, signed_out=True)], readiness_only=False)
        driver.get.assert_called_once_with(LOGIN_URL)
        self.assertNotIn(("table_tennis", True), events)
        self.client.signals.get.assert_not_called()

    def test_browser_closed_during_login_is_detected_before_any_signal_or_wager(self):
        from selenium.common.exceptions import InvalidSessionIdException
        self.run_startup([InvalidSessionIdException("browser closed")], readiness_only=False)
        self.assertTrue(self.client.browser_alerted)
        self.assertFalse(self.client.lease.running)
        self.client.authorize.assert_not_called()

    def test_login_only_start_waits_for_auth_and_location_then_stays_non_actionable(self):
        _, events = self.run_startup([
            self.state(False, signed_out=True), self.state(False, signed_in=True),
            self.state(True, signed_in=True), self.state(True, signed_in=True)], readiness_only=True)
        self.assertEqual(events, [("provider", False), ("provider", False), ("provider", True),
                                  ("table_tennis", True), ("provider", True)])
        self.client.signals.get.assert_not_called()
        self.assertFalse(self.client.provider_ready.is_set())
        self.assertEqual(self.client.device_test.state["outcome"], "login_ready_no_wagers")
        self.assertFalse(self.client.device_test.claim({"id": 1, "payload": {"event": {"eventId": "1"}}}))

    def test_normal_signal_wait_starts_only_after_post_navigation_auth_recheck(self):
        _, events = self.run_startup([self.state(False, signed_out=True), self.state(True, signed_in=True),
            self.state(False), self.state(True, signed_in=True)], readiness_only=False)
        self.assertEqual(events, [("provider", False), ("provider", True), ("table_tennis", True),
                                  ("provider", False), ("provider", True), ("signal_wait", True)])
        self.assertFalse(self.client.provider_ready.is_set())

    def test_live_board_holds_initial_league_navigation_but_still_rechecks_provider(self):
        self.client.scanner = {'board': {'events': [{'event_id': '100'}]}}
        _, events = self.run_startup([self.state(True, signed_in=True), self.state(True, signed_in=True)], readiness_only=False)
        self.assertEqual(events, [('provider', True), ('provider', True), ('signal_wait', True)])

    def test_scan_only_can_prepare_a_verified_match_without_reading_or_claiming_a_wager(self):
        now = datetime.now(timezone.utc)
        self.client.last_feed = True
        self.client.preparation = {"protocol": 1, "event": {"eventId": "100", "player1": "One", "player2": "Two"},
            "market_hint": {"betType": "closeout"}, "expires_at": (now+timedelta(seconds=15)).isoformat()}
        _, events = self.run_startup([self.state(True, signed_in=True), self.state(True, signed_in=True)],
                                     readiness_only=True, scan_only=True)
        self.client.signals.get.assert_not_called()
        self.assertEqual(self.client.device_test.state["mode"], "scanner-navigation-readiness")
        self.assertEqual(self.client.device_test.state["prepared_event_id"], "100")
        self.assertEqual(self.client.device_test.state["opportunities"], 0)
        self.assertFalse(self.client.device_test.state["submitted"])
        self.assertEqual(self.client.device_test.state["outcome"], "scanner_navigation_ready_no_wagers")

class SlipStagingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": directory.name,
            "EMBER_DEVICE_TEST_ONCE": "", "EMBER_DEVICE_TEST_LOGIN_ONLY": "", "EMBER_DEVICE_TEST_SCAN_ONLY": "",
            "EMBER_DEVICE_BROWSER_TICKET": ""}))
        self.stack.enter_context(patch.dict(sys.modules, {"keyring": Mock()}))
        self.client = Client()
        self.client.account = "test-account"
        now = datetime.now(timezone.utc)
        self.client.lease.update({"enabled": True, "client_supported": True, "server_time": now.isoformat(),
                                  "access_until": (now+timedelta(hours=1)).isoformat(), "lease_seconds": 5})
        self.client.last_feed = True
        self.event = {"eventId": "100", "player1": "Piotr Zemla", "player2": "Tomasz Lojtek"}
        self.client.lease.running = True
        self.client.provider_ready.set()
        self.inventory = {"status": "present", "stagingSafe": True, "legs": []}
        self.stack.enter_context(patch('ember_staging.read_plan_slip', return_value=self.inventory))
        self.client.preparation = {"protocol": 1, "event": self.event, "market_hint": {"betType": "split"},
                                   "expires_at": (now+timedelta(seconds=15)).isoformat()}
        import tt_market_exec as core
        self.core = core
        for name, value in (("clear_slip", Mock(return_value=True)),
                            ("click_selection", Mock(return_value={"status": "clicked"})),
                            ("ensure_slip_open", Mock(return_value=True)),
                            ("score_state", Mock(return_value=[1, 0]))):
            self.stack.enter_context(patch.object(core, name, value))
        self.driver = Mock()
        self.browser_preparation = Mock(key=("100", "split"))
        self.market_readiness = Mock()
        self.market_readiness.open.return_value = {"status": "opened"}
        self.market_readiness.wait.return_value = {"status": "revealed"}
        self.market_readiness.prepare.return_value = {"status": "revealed"}
        self.hint = ClientProtocolTests.stage(event_id="100", event=self.event)
        self.client.scanner = {"healthy": True, "counts": {}, "stages": [self.hint]}

    def maintain(self, mono=1.0):
        return self.client.maintain_staging(self.driver, self.core, self.browser_preparation,
                                            self.market_readiness, mono)

    def test_hinted_selection_is_pre_opened_without_a_stake_or_submit(self):
        self.client.stage_due = 0.0
        staged = self.maintain()
        self.assertEqual(staged, self.hint)
        self.core.click_selection.assert_called_once()
        driver, event, outcome, market = self.core.click_selection.call_args.args[:4]
        self.assertEqual(event["eventId"], "100")
        self.assertEqual((outcome["selectionId"], outcome["label"], outcome["points"]),
                         ("s2", "Tomasz Lojtek", None))
        self.assertEqual((market["name"], market["marketId"]), ("Game 2", "200"))
        # Staging clicks the odds button only; no stake write, no submit, no bettor.
        self.assertLessEqual(self.core.click_selection.call_args.kwargs["timeout"], .3)
        self.assertIsNone(self.client.bettor)

    def test_stale_hint_never_stages_or_clears_existing_legs(self):
        self.client.stage_due = 0.0
        self.client.scanner = {"healthy": True, "counts": {},
                               "stages": [ClientProtocolTests.stage(event_id="100", event=self.event,
                                    expires_at=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),
                                    updated_at=(datetime.now(timezone.utc)-timedelta(seconds=30)).isoformat())]}
        self.assertIsNone(self.maintain())
        self.core.click_selection.assert_not_called()
        self.core.clear_slip.assert_not_called()
        self.inventory['legs'] = [{'eventId': '100', 'marketId': '200', 'selectionId': 's2'}]
        self.assertIsNone(self.maintain())
        self.core.clear_slip.assert_not_called()
        self.assertTrue(self.client.hold_preparation())

    def test_a_moved_hint_adds_a_leg_without_clearing_others(self):
        self.client.stage_due = 0.0
        self.inventory['legs'] = [{'eventId': '100', 'marketId': '200', 'selectionId': 's2'}]
        self.client.scanner['stages'] = [ClientProtocolTests.stage(event_id='100', selection_id='s1', event=self.event)]
        moved = self.maintain()
        self.assertEqual(moved["selection_id"], "s1")
        self.assertEqual(self.core.click_selection.call_args.args[2]["selectionId"], "s1")
        self.core.clear_slip.assert_not_called()

    def test_full_hint_can_stage_another_event_without_preparation_authority(self):
        self.client.stage_due = 0.0
        self.browser_preparation.key = ("999", "closeout")
        self.assertEqual(self.maintain(), self.hint)
        self.assertEqual(self.market_readiness.prepare.call_args.args[1], self.event)

    def test_unhealthy_feed_never_stages(self):
        self.client.stage_due = 0.0
        self.client.last_feed = False
        self.assertIsNone(self.maintain())
        self.core.click_selection.assert_not_called()


class PlanRenewalTests(unittest.TestCase):
    setUp = ProtocolV2Tests.setUp
    plan_signal = staticmethod(plan_signal)

    def status(self, signal=None):
        now = datetime.now(timezone.utc)
        return {'enabled': True, 'client_supported': True, 'server_time': now.isoformat(),
                'access_until': (now+timedelta(hours=1)).isoformat(), 'lease_seconds': 5,
                **({'signal': signal} if signal else {})}

    def test_expired_envelope_can_renew_before_expiry_gate_and_preserves_plan_reference(self):
        from copy import deepcopy
        signal = self.plan_signal()
        renewed = deepcopy(signal)
        signal['expires_at'] = signal['payload']['plan']['deadline'] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        plan = signal['payload']['plan']
        self.client.provider_ready.set()
        with patch.object(self.client, 'request', return_value=self.status(renewed)) as request:
            self.assertTrue(self.client.authorize(signal))
        self.assertIs(signal['payload']['plan'], plan)
        self.assertEqual(plan['deadline'], renewed['payload']['plan']['deadline'])
        self.assertEqual(signal['expires_at'], renewed['expires_at'])
        self.assertLessEqual(request.call_args.kwargs['timeout'], .3)

    def test_renewal_rejects_contract_selection_score_or_plan_drift_atomically(self):
        from copy import deepcopy
        changes = [lambda s: s.update(id=2), lambda s: s.update(contract='other'),
                   lambda s: s['payload']['market'].update(marketId='999'),
                   lambda s: s['payload']['plan'].update(selectionId='301'),
                   lambda s: s['payload']['plan'].update(min_decimal=2),
                   lambda s: s['payload']['plan'].update(stake_cents=500),
                   lambda s: s['payload']['event']['currentSet'].update(home=2)]
        for change in changes:
            with self.subTest(change=changes.index(change)):
                signal = self.plan_signal()
                saved = deepcopy(signal)
                renewed = deepcopy(signal)
                change(renewed)
                with self.assertRaises((ValueError, KeyError)):
                    self.client.renew_signal(signal, {'signal': renewed})
                self.assertEqual(signal, saved)

    def test_quote_reserves_durably_before_claim_and_renewal_updates_the_envelope(self):
        from copy import deepcopy
        self.client.provider_ready.set()
        signal = self.plan_signal()
        plan = signal['payload']['plan']
        renewed = deepcopy(signal)
        renewed['expires_at'] = (datetime.now(timezone.utc)+timedelta(seconds=6)).isoformat()
        renewed['payload']['plan']['deadline'] = renewed['expires_at']
        ledger = Ledger(self.client.data_dir/'execution.sqlite3')
        self.addCleanup(ledger.connection.close)
        calls = []
        def request(*args, **kwargs):
            calls.append(args)
            self.assertEqual(ledger.connection.execute('SELECT status FROM attempts').fetchall(), [('reserved_or_unconfirmed',)])
            return {**self.status(renewed), 'plan': {'stake_cents': 261, 'min_decimal': 1.85, 'echo': 'fixture'}}
        claim = self.client.claim_values(signal, ledger)
        with patch.object(self.client, 'request', side_effect=request):
            self.assertEqual(claim({'balance': {}, 'decimalOdds': 2.5}), {'denied': 'sizing_failed'})
            self.assertEqual(ledger.connection.execute('SELECT * FROM attempts').fetchall(), [])
            for _ in range(2):
                self.assertEqual(claim({'balance': {'availableBalance': 100}, 'decimalOdds': 2.5})['stake'], 2.61)
        self.assertEqual(len(calls), 2)
        self.assertEqual(signal['expires_at'], renewed['expires_at'])
        self.assertIs(signal['payload']['plan'], plan)
        self.assertEqual(plan['deadline'], renewed['expires_at'])
        self.assertFalse(self.client.defer_readiness(signal, {'retryable': True}))
        duplicate = self.plan_signal()
        with patch.object(self.client, 'request') as request:
            self.assertEqual(self.client.claim_values(duplicate, ledger)(
                {'balance': {'availableBalance': 100}, 'decimalOdds': 2.5}), {'denied': 'already_attempted'})
        request.assert_not_called()

    def test_response_without_renewed_signal_never_invents_a_new_deadline(self):
        signal = self.plan_signal()
        saved = signal['expires_at']
        self.client.renew_signal(signal, self.status())
        self.assertEqual(signal['expires_at'], saved)
        signal['expires_at'] = signal['payload']['plan']['deadline'] = (datetime.now(timezone.utc)+timedelta(seconds=self.client.lease.server_offset+.05)).isoformat()
        self.assertLessEqual(self.client.authorization_timeout(signal), .051)

    def test_preclaim_timeout_yields_without_revoking_a_positive_feed_lease(self):
        import requests
        self.client.provider_ready.set()
        signal = self.plan_signal()
        until = self.client.lease.until
        with patch.object(self.client, 'request', side_effect=requests.Timeout):
            self.assertFalse(self.client.authorize(signal))
        self.assertEqual(self.client.lease.until, until)
        self.assertEqual(self.client.last_authorize_reason, 'authorization_unavailable')
        other = self.plan_signal()
        other.update(id=2, contract='other')
        self.client.signals.put_latest(other)
        self.assertTrue(self.client.defer_readiness(signal, {'status': 'authorization_timeout'}))
        self.assertIs(self.client.signals.get_nowait(), other)
        self.assertIs(self.client.signals.get_nowait(), signal)

    def test_expired_plan_cannot_consume_a_durable_reservation_even_with_live_envelope(self):
        self.client.provider_ready.set()
        signal = self.plan_signal(deadline=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat())
        ledger = Mock()
        with patch.object(self.client, 'request') as request:
            result = self.client.claim_values(signal, ledger)({'balance': {'availableBalance': 100}, 'decimalOdds': 2.5})
        self.assertEqual(result, {'denied': 'signal_expired'})
        ledger.reserve_signal.assert_not_called()
        request.assert_not_called()

    def test_quote_claim_can_wait_for_its_reply_without_lengthening_preclaim_turns(self):
        self.client.provider_ready.set()
        signal = self.plan_signal()
        status = {**self.status(), 'plan': {'stake_cents': 261, 'min_decimal': 1.85, 'echo': 'fixture'}}
        with patch.object(self.client, 'request', return_value=status) as request:
            self.assertTrue(self.client.authorize(signal))
            self.assertEqual(self.client.claim_values(signal)(
                {'balance': {'availableBalance': 100}, 'decimalOdds': 2.5})['stake'], 2.61)
        preclaim, claim = [call.kwargs['timeout'] for call in request.call_args_list]
        self.assertLessEqual(preclaim, .3)
        self.assertGreater(claim, preclaim)
        self.assertLessEqual(claim, 1)
        signal['payload']['plan']['deadline'] = (datetime.now(timezone.utc)+timedelta(seconds=self.client.lease.server_offset+.05)).isoformat()
        self.assertLessEqual(self.client.authorization_timeout(signal), .051)


if __name__ == "__main__":
    unittest.main()
