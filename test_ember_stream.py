from datetime import datetime, timedelta, timezone
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import websocket

from ember_client import Client


class StreamRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with patch.dict(os.environ, {"LOCALAPPDATA": directory.name, "EMBER_DEVICE_TEST_ONCE": "",
            "EMBER_DEVICE_TEST_LOGIN_ONLY": "", "EMBER_DEVICE_TEST_SCAN_ONLY": ""}), patch.dict(sys.modules, {"keyring": Mock()}):
            self.client = Client()
        self.client.api = "https://example.com"
        self.client.conservative_level='conservative'
        self.client.access_token = "fixture-access"
        self.client.token_until = time.monotonic()+1000
        self.client.ensure_access = Mock()
        self.client.request = Mock(return_value={"enabled": True, "client_supported": True,
            "stream_url": "wss://feeds.example.com/api/v1/signals/stream/"})
        self.client.stop.wait = Mock(side_effect=lambda _: self.client.stop.is_set())
        now = datetime.now(timezone.utc)
        self.lease = {"type": "lease", "enabled": True, "client_supported": True, "feed_healthy": True,
            "server_time": now.isoformat(), "access_until": (now+timedelta(hours=1)).isoformat(), "lease_seconds": 5,'window_ids':[1,2]}

    def socket(self, frames, fail_close=False, before_receive=None):
        incoming = iter(frames)
        socket = Mock()
        def receive():
            try:
                item = next(incoming)
            except StopIteration:
                self.client.stop.set()
                return ""
            if before_receive:
                before_receive()
            if isinstance(item, BaseException):
                raise item
            return json.dumps(item)
        socket.recv.side_effect = receive
        if fail_close:
            socket.close.side_effect = OSError("fixture close failure")
        return socket

    def test_socket_close_failure_cannot_kill_reconnection_or_hide_a_restored_feed(self):
        first = self.socket([self.lease, websocket.WebSocketConnectionClosedException()], fail_close=True)
        second = self.socket([self.lease])
        with patch("ember_client.websocket.create_connection", side_effect=[first, second]) as connect, patch("ember_client.emit") as emit:
            self.client.stream()
        self.assertEqual(connect.call_count, 2)
        connected = [call for call in emit.call_args_list if call.args == ("status",) and call.kwargs.get("message") == "Live signal feed connected."]
        self.assertEqual(len(connected), 2)
        self.assertIsNone(self.client.preparation)
        self.assertFalse(self.client.stream_connected)

    def test_soft_token_expiry_refreshes_and_reconnects_with_the_new_token(self):
        first = self.socket([self.lease], before_receive=lambda: setattr(self.client, "token_until", time.monotonic()-1))
        second = self.socket([self.lease])
        ensures = [0]
        def ensure():
            ensures[0] += 1
            if ensures[0] >= 2:
                self.client.access_token = "fixture-refreshed"
                self.client.token_until = time.monotonic()+1000
        self.client.ensure_access.side_effect = ensure
        with patch("ember_client.websocket.create_connection", side_effect=[first, second]), patch("ember_client.emit") as emit:
            self.client.stream()
        self.assertEqual(json.loads(second.send.call_args.args[0])["token"], "fixture-refreshed")
        messages = [call.kwargs.get("message", "") for call in emit.call_args_list]
        self.assertTrue(any("Refreshing Ember sign-in" in message for message in messages))
        self.assertFalse(any("Signal access paused" in message for message in messages))

    def test_invalid_metrics_do_not_break_a_valid_lease_and_hint(self):
        with patch("ember_client.emit"):
            self.client.accept_lease({**self.lease, "scanner": {"healthy": True, "counts": {"events": True}}})
        self.assertIsNone(self.client.scanner)
        self.assertTrue(self.client.stream_connected)
        self.assertGreater(self.client.lease.until, self.client.lease.clock())

    def test_qualified_signal_received_before_provider_readiness_is_persisted(self):
        from test_ember_history import history_signal
        self.client.account = "1"
        self.client.lease.running = True
        signal = history_signal()
        self.client.receive_opportunity(signal, self.client.history_scope())
        self.client.history.close_pending(self.client.history_scope(), now=(datetime.now(timezone.utc)+timedelta(seconds=10)).isoformat(), expired_only=True)
        row = self.client.history.snapshot(self.client.history_scope())["records"][0]
        self.assertEqual(row["outcome"], "missed")
        self.assertEqual(row["reason"], "login_required")
        self.assertTrue(self.client.signals.empty())

    def test_stream_keeps_the_latest_quote_for_a_waiting_contract(self):
        from copy import deepcopy
        from test_ember_client import window_signal
        self.client.account = '1'
        self.client.lease.running = True
        self.client.provider_ready.set()
        first = window_signal()
        latest = deepcopy(first)
        latest['id'] = 2
        latest['payload']['plan']['min_decimal'] = 3
        latest['payload']['market']['outcomes'][0]['decimal']=3
        connection = self.socket([self.lease, {'type': 'signal', 'signal': first},
                                  {'type': 'signal', 'signal': latest}])
        observed = []
        disconnect = self.client.feed_disconnected
        def capture(*args):
            observed.extend(list(self.client.signals.queue))
            disconnect(*args)
        with patch('ember_client.websocket.create_connection', return_value=connection), \
                patch.object(self.client, 'feed_disconnected', side_effect=capture), patch('ember_client.emit'):
            self.client.stream()
        self.assertEqual([signal['id'] for signal in observed], [2])
        self.assertEqual(observed[0]['payload']['plan']['min_decimal'], 3)

    def test_malformed_opportunity_cannot_disconnect_or_hide_the_following_valid_signal(self):
        from copy import deepcopy
        from test_ember_client import window_signal
        self.client.account='1';self.client.lease.running=True
        good=window_signal();bad=deepcopy(good)
        bad['payload']['plan']['selectionId']=None
        connection=self.socket([self.lease,{'type':'signal','signal':bad},{'type':'signal','signal':good}])
        observed=[]
        disconnect=self.client.feed_disconnected
        def capture(*args):
            observed.extend(list(self.client.signals.queue));disconnect(*args)
        with patch('ember_client.websocket.create_connection',return_value=connection) as connect, \
                patch.object(self.client,'feed_disconnected',side_effect=capture),patch('ember_client.emit') as emit:
            self.client.stream()
        self.assertEqual(connect.call_count,1)
        self.assertEqual([signal['id'] for signal in observed],[good['id']])
        self.assertTrue(any(call.args==('signal_rejected',) for call in emit.call_args_list))
        self.assertNotIn('fixture-access',str(emit.call_args_list))

    def test_stream_cannot_replace_the_selected_confidence_with_the_other_window(self):
        from test_ember_client import window_signal
        self.client.account='1';self.client.lease.running=True;self.client.provider_ready.set()
        for level,confidence in (('conservative',90),('very_conservative',95)):
            with self.subTest(level=level):
                self.client.stop.clear();self.client.conservative_level=level
                signals=[window_signal(confidence=n) for n in (90,95)]
                signals[1]['id']=2
                connection=self.socket([self.lease,*[{'type':'signal','signal':signal} for signal in signals]])
                observed=[];disconnect=self.client.feed_disconnected
                def capture(*args):
                    observed.extend(list(self.client.signals.queue));disconnect(*args)
                with patch('ember_client.websocket.create_connection',return_value=connection), \
                        patch.object(self.client,'feed_disconnected',side_effect=capture),patch('ember_client.emit'):
                    self.client.stream()
                self.assertEqual(len(observed),1)
                self.assertEqual(observed[0]['payload']['plan']['window']['confidence'],confidence)

    def test_sync_failure_retains_local_outbox_and_retry_is_acknowledged(self):
        from test_ember_history import history_signal
        self.client.account = "1"
        self.client.lease.running = False
        self.client.receive_opportunity(history_signal(), self.client.history_scope())
        session = Mock()
        self.client.http_local.session = session
        session.request.return_value.status_code = 503
        with self.assertRaisesRegex(RuntimeError, "history_sync_unavailable"):
            self.client.sync_history_once()
        self.assertEqual(self.client.history.snapshot(self.client.history_scope())["queued"], 1)
        batch = self.client.history.pending_batch(self.client.history_scope())
        session.request.return_value.status_code = 200
        session.request.return_value.json.return_value = {"acknowledged": [{"id": row["id"], "revision": row["revision"]} for row in batch]}
        with patch("ember_client.emit"):
            self.assertTrue(self.client.sync_history_once())
        self.assertEqual(self.client.history.snapshot(self.client.history_scope())["queued"], 0)
        request = session.request.call_args
        self.assertEqual(request.args[1], "https://example.com/api/v1/missed-bets/")
        self.assertEqual(request.kwargs["json"]["account_id"], "1")

    def test_account_change_during_token_refresh_cannot_upload_the_old_accounts_logs(self):
        from test_ember_history import history_signal
        self.client.account = "1"
        self.client.lease.running = False
        old_scope = self.client.history_scope()
        self.client.receive_opportunity(history_signal(), old_scope)
        session = self.client.http_local.session = Mock()
        def change_account():
            self.client.api = "https://other.example"
            self.client.account = "2"
            self.client.access_token = "other-fixture-token"
        self.client.ensure_access.side_effect = change_account
        self.assertFalse(self.client.sync_history_once())
        session.request.assert_not_called()
        self.assertEqual(self.client.history.snapshot(old_scope)["queued"], 1)


if __name__ == "__main__":
    unittest.main()
