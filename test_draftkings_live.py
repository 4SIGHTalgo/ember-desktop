import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import msgpack
import websocket

import draftkings_live as live


T0 = "2026-10-17T05:49:00+00:00"
T1 = "2026-10-17T05:49:01+00:00"


def event(home=9, away=8, period="4th"):
    card = [[None, None, 2147483647, "2", "1", None, None], [
        [None, "1", 1, "11", "7", None, None],
        [None, "2", 2, "7", "11", None, None],
        [None, "3", 3, "11", "8", None, None],
        [None, "4", 4, str(home), str(away), None, None],
        [None, "5", 5, "", "", None, None],
        [None, None, 2147483647, "2", "1", None, None],
    ], 1]
    return [16, ["34843465", "26", "208037", "TT Elite Series", "Alice vs Bob", None,
                 "Alice", "Bob", ["STARTED", False, period, 0, 0, False, "2", "1"], [], False, card]]


def market(suspended=False, american="-154", tag=17):
    return [tag, ["379827916", "Game 4", "34843465", "208037", "26", 6386, 2, suspended,
                  [["selection1", "Alice", "379827916", american, 1.64935065, "1.64", "50/77", None, "Alice"]]]]


def update(events=(), markets=(), remove_events=(), remove_markets=(), add_markets=()):
    return ["subscription", "update", [
        [[[], list(add_markets), None], [list(remove_events), list(remove_markets), None],
         [list(events), list(markets), None]], None, {"publishedTime": "2026-10-17T05:49:00.010Z"},
    ], None, []]


class StateTests(unittest.TestCase):
    def test_real_wire_layout_scores_exclude_summary_and_blank_future_sets(self):
        state = live.LiveState()
        message = msgpack.unpackb(msgpack.packb(update(events=[event(10, 10)])), raw=False)
        records = state.apply(message, T0)
        self.assertEqual(records[0]["currentSet"], {"set": 4, "home": 10, "away": 10})
        self.assertEqual(records[0]["setTally"], [2, 1])
        self.assertEqual(len(records[0]["sets"]), 4)
        self.assertFalse(any(r["type"] == "point10" for r in records))

    def test_add_and_change_market_preserve_identity_and_suspension(self):
        state = live.LiveState()
        state.apply(update(add_markets=[market(tag=27)]), T0)
        rows = state.apply(update(markets=[market(suspended=True, american="+108")]), T1)
        self.assertEqual(rows[0]["marketId"], "379827916")
        self.assertEqual(rows[0]["eventId"], "34843465")
        self.assertTrue(rows[0]["isSuspended"])
        self.assertEqual(rows[0]["outcomes"][0]["american"], "+108")

    def test_predicate_uses_latest_market_after_entire_delta(self):
        state = live.LiveState()
        state.apply(update(events=[event()], markets=[market()]), T0)
        rows = state.apply(update(events=[event(10)], markets=[market(american="-110")]), T1)
        trigger = next(r for r in rows if r["type"] == "point10")
        self.assertEqual(trigger["player"], "Alice")
        self.assertEqual(trigger["markets"][0]["outcomes"][0]["american"], "-110")
        self.assertEqual(trigger["markets"][0]["quoteAgeMs"], 0)
        self.assertFalse(trigger["marketStateComplete"])
        self.assertEqual(state.apply(update(events=[event(10)]), T1), [])

    def test_both_players_and_cached_quote_age(self):
        state = live.LiveState()
        state.apply(update(events=[event(9, 9)], markets=[market(suspended=True)]), T0)
        rows = state.apply(update(events=[event(10, 10)]), T1)
        triggers = [r for r in rows if r["type"] == "point10"]
        self.assertEqual([r["player"] for r in triggers], ["Alice", "Bob"])
        self.assertEqual(triggers[0]["markets"][0]["quoteAgeMs"], 1000)
        self.assertTrue(triggers[0]["markets"][0]["isSuspended"])

    def test_removed_market_cannot_be_reported_as_current(self):
        state = live.LiveState()
        state.apply(update(events=[event()], markets=[market()]), T0)
        rows = state.apply(update(events=[event(10)], remove_markets=["379827916"]), T1)
        trigger = next(r for r in rows if r["type"] == "point10")
        self.assertEqual(trigger["markets"], [])
        state.apply(update(markets=[market()]), T1)
        state.apply(update(remove_events=["34843465"]), T1)
        self.assertEqual(state.events, {})
        self.assertEqual(state.markets, {})

    def test_no_trigger_across_sets_or_for_unobserved_ten(self):
        state = live.LiveState()
        state.apply(update(events=[event()]), T0)
        for row in (event(10, period="3rd"), event(11)):
            records = state.apply(update(events=[row]), T1)
            self.assertFalse(any(r["type"] == "point10" for r in records))

    def test_malformed_frames_and_server_rejection_stop(self):
        state = live.LiveState()
        for message in ([], ["id", "unexpected", None, None], update(events=[[16, []]])):
            with self.subTest(message=message), self.assertRaises(ValueError):
                state.apply(message, T0)
        with self.assertRaises(RuntimeError):
            state.apply(["id", "error", None, [403, None, "Denied"]], T0)


class ConnectionTests(unittest.TestCase):
    def test_reconnect_discards_cached_state_and_resubscribes(self):
        class Socket:
            def __init__(self, messages):
                self.messages = iter(messages)
                self.sent = []
                self.closed = False
            def settimeout(self, timeout):
                pass
            def send_binary(self, frame):
                self.sent.append(msgpack.unpackb(frame, raw=False))
            def recv(self):
                item = next(self.messages)
                if isinstance(item, BaseException):
                    raise item
                return msgpack.packb(item)
            def close(self):
                self.closed = True
        first = Socket([update(events=[event()], markets=[market()]), websocket.WebSocketConnectionClosedException()])
        second = Socket([update(events=[event(10)]), KeyboardInterrupt()])
        records = []
        with patch.object(live.websocket, "create_connection", side_effect=[first, second]), \
                patch.object(live.time, "sleep"), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                live.capture(SimpleNamespace(duration=0, event_id=None), records.append)
        self.assertEqual(sum(r["type"] == "reset" for r in records), 2)
        self.assertFalse(any(r["type"] == "point10" for r in records))
        self.assertTrue(first.closed and second.closed)
        self.assertNotEqual(first.sent[0]["id"], second.sent[0]["id"])

    def test_subscription_is_public_read_only_and_can_filter_event(self):
        request = live.subscription("34843465")
        self.assertEqual(request["method"], "subscribe")
        self.assertEqual(request["params"]["jwt"], "default-token")
        self.assertEqual(request["params"]["forwardedHeaders"], {})
        self.assertEqual(request["params"]["queryParams"]["query"], "$filter=id eq '34843465'")

    def test_subscription_ack_reports_connection_without_inventing_scores_or_quotes(self):
        class Socket:
            def send_binary(self, frame): pass
            def settimeout(self, timeout): pass
            def close(self): pass
            def recv(self):
                if not hasattr(self, "sent_ack"):
                    self.sent_ack = True
                    return msgpack.packb(["id", "subscribed", None, None])
                raise KeyboardInterrupt
        records = []
        with patch.object(live.websocket, "create_connection", return_value=Socket()):
            with self.assertRaises(KeyboardInterrupt):
                live.capture(SimpleNamespace(duration=0, event_id=None), records.append)
        self.assertEqual([row["type"] for row in records], ["reset", "connected"])
        self.assertFalse(records[-1]["marketStateComplete"])

    def test_cli_rejects_invalid_duration_or_event_before_connecting(self):
        for arguments in (["--duration", "nan"], ["--duration", "-1"], ["--event-id", "bad'id"]):
            with self.subTest(arguments=arguments), patch("sys.argv", ["script", *arguments]), \
                    patch.object(live, "capture") as capture, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    live.main()
                self.assertEqual(error.exception.code, 2)
                capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
