r"""Read-only TT Elite score/odds stream; no login, saved cookies, or betting.

Run: .\.venv\Scripts\python.exe draftkings_live.py
Use --duration 120 for a bounded capture, or --event-id ID for one match.
Records go to dk_live.jsonl. Scores and odds are independent updates; cached
quotes include timestamps and suspension status, not a promise of execution.

Wire mapping follows DraftKings' public dk-data-layer/4.0.0/dkDataLayer.js,
functions Hr/Gn/lr/cr (betOffers projection). This is an undocumented API.
"""

import argparse
from contextlib import closing
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import re
import sys
import time
import uuid

import msgpack
import websocket


SOCKET_URL = "wss://sportsbook-ws-us-md.draftkings.com/websocket?format=msgpack&locale=en"
ORIGIN = "https://sportsbook.draftkings.com"
LEAGUE_ID = "208037"


class FeedRejected(RuntimeError):
    """A rejected subscription/access check must not be retried in a tight loop."""


def retry_after_seconds(headers, default=60, now=None):
    """Honor numeric or HTTP-date Retry-After, keeping a local minimum backoff."""
    value = next((value for key, value in (headers or {}).items()
                  if str(key).lower() == "retry-after"), None)
    if value is None:
        return float(default)
    try:
        seconds = float(value)
        if math.isfinite(seconds) and seconds >= 0:
            return max(float(default), seconds)
    except (TypeError, ValueError):
        pass
    try:
        date = parsedate_to_datetime(str(value))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return max(float(default), date.timestamp() - (time.time() if now is None else now))
    except (TypeError, ValueError, OverflowError):
        return float(default)


def subscription(event_id=None):
    query = f"$filter=id eq '{event_id}'" if event_id else f"$filter=leagueId eq '{LEAGUE_ID}'"
    return {
        "jsonrpc": "2.0", "method": "subscribe", "id": str(uuid.uuid4()),
        "params": {
            "entity": "events",
            "queryParams": {
                "query": query, "initialData": False, "projection": "betOffers", "locale": "en",
                "includeMarkets": "$filter=tags/all(t: t ne 'SportcastBetBuilder') and tags/any(t: t eq 'OSB')",
            },
            "forwardedHeaders": {},
            "clientMetadata": {"feature": "league", "X-Client-Name": "web", "X-Client-Version": "2642.4.1.4"},
            # This literal is the SDK's public, signed-out value, not an account token.
            "jwt": "default-token", "siteName": "dkusmd",
        },
    }


def point(value):
    return int(value) if isinstance(value, str) and value.isascii() and value.isdigit() else None


def event_record(row):
    if not isinstance(row, list) or len(row) < 12 or not isinstance(row[8], list) or len(row[8]) < 8:
        raise ValueError("Unknown event schema")
    status = row[8]
    card = row[11]
    sets = []
    for part in (card[1] or []) if card else []:
        if len(part) < 5:
            raise ValueError("Unknown score schema")
        if isinstance(part[2], int) and 1 <= part[2] <= 5:
            home, away = point(part[3]), point(part[4])
            if home is not None and away is not None:
                sets.append({"set": part[2], "home": home, "away": away})
    period = re.fullmatch(r"([1-5])(?:st|nd|rd|th)", status[2] or "")
    current = next((s for s in sets if period and s["set"] == int(period[1])), None)
    return {
        "eventId": str(row[0]), "leagueId": str(row[2]), "player1": row[6], "player2": row[7],
        "startTimestamp": row[5].seconds if isinstance(row[5], msgpack.Timestamp) else None,
        "status": status[0], "period": status[2], "setTally": [point(status[6]), point(status[7])],
        "sets": sets, "currentSet": current,
    }


def market_record(row):
    if not isinstance(row, list) or len(row) < 9:
        raise ValueError("Unknown market schema")
    outcomes = []
    for outcome in row[8] or []:
        if not isinstance(outcome, list) or len(outcome) < 9:
            raise ValueError("Unknown outcome schema")
        outcomes.append({
            "selectionId": str(outcome[0]), "label": outcome[1],
            "american": outcome[3], "decimal": outcome[4], "points": outcome[7],
        })
    return {
        "marketId": str(row[0]), "name": row[1], "eventId": str(row[2]),
        "subcategoryId": row[5], "isSuspended": row[7], "outcomes": outcomes,
    }


class LiveState:
    def __init__(self):
        self.events = {}
        self.markets = {}

    def apply(self, message, received_at):
        if not isinstance(message, list) or len(message) < 4:
            raise ValueError("Unknown socket envelope")
        if message[3] is not None:
            raise FeedRejected("DraftKings rejected the subscription; stopping")
        if message[1] in ("subscribed", "unsubscribed"):
            return []
        if message[1] != "update":
            raise ValueError("Unknown socket message type")
        payload = message[2]
        if not isinstance(payload, list) or len(payload) < 3:
            raise ValueError("Unknown update schema")
        changes, _, metadata = payload[:3]
        if not isinstance(changes, list) or len(changes) != 3:
            raise ValueError("Unknown change schema")
        published = (metadata or {}).get("publishedTime")
        timing = {"receivedAt": received_at, "publishedAt": published}
        records, pending_scores = [], []
        # Apply the entire delta before evaluating the point predicate.
        for operation, groups in zip(("add", "remove", "change"), changes):
            if not isinstance(groups, list) or len(groups) < 2:
                raise ValueError("Unknown entity groups")
            for entity, rows in zip(("event", "market"), groups[:2]):
                for item in rows or []:
                    state = self.events if entity == "event" else self.markets
                    if operation == "remove":
                        identifier = str(item)
                        state.pop(identifier, None)
                        if entity == "event":
                            self.markets = {k: m for k, m in self.markets.items() if m["eventId"] != identifier}
                        records.append({"type": entity + "Removed", entity + "Id": identifier, **timing})
                        continue
                    if not isinstance(item, list) or len(item) != 2:
                        raise ValueError("Unsupported entity wire type")
                    value = event_record(item[1]) if entity == "event" else market_record(item[1])
                    identifier = value[entity + "Id"]
                    previous = state.get(identifier)
                    value.update(timing)
                    state[identifier] = value
                    if entity == "market":
                        records.append({"type": "market", **value})
                    elif previous is None or any(previous[k] != value[k] for k in ("sets", "status", "period", "setTally")):
                        records.append({"type": "score", **value})
                        pending_scores.append((previous, value))
        for previous, event in pending_scores:
            old = previous and previous["currentSet"]
            current = event["currentSet"]
            if not old or not current or old["set"] != current["set"] or event["status"] != "STARTED":
                continue
            for side, player in (("home", "player1"), ("away", "player2")):
                if old[side] < 10 and current[side] == 10:
                    quotes = []
                    for market in self.markets.values():
                        if market["eventId"] == event["eventId"]:
                            age = datetime.fromisoformat(received_at) - datetime.fromisoformat(market["receivedAt"])
                            quotes.append({**market, "quoteAgeMs": round(age.total_seconds() * 1000, 3)})
                    records.append({
                        "type": "point10", "eventId": event["eventId"], "player": event[player],
                        "currentSet": current, "markets": quotes, "marketStateComplete": False, **timing,
                    })
        return records


def capture(args, emit, stop_event=None):
    deadline = time.monotonic() + args.duration if args.duration else float("inf")
    delay = 1
    while time.monotonic() < deadline and not (stop_event and stop_event.is_set()):
        # The feed is incremental: reset cached quotes after any disconnect.
        state = LiveState()
        emit({"type": "reset", "receivedAt": datetime.now(timezone.utc).isoformat(), "marketStateComplete": False})
        try:
            with closing(websocket.create_connection(SOCKET_URL, origin=ORIGIN, timeout=min(10, max(.1, deadline-time.monotonic())))) as ws:
                ws.send_binary(msgpack.packb(subscription(args.event_id), use_bin_type=True))
                connected_at = last_frame = time.monotonic()
                while time.monotonic() < deadline and not (stop_event and stop_event.is_set()):
                    ws.settimeout(min(5, max(.1, deadline-time.monotonic())))
                    try:
                        frame = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        if time.monotonic() - last_frame > 30:
                            raise websocket.WebSocketConnectionClosedException("Feed idle")
                        continue
                    if not frame:
                        raise websocket.WebSocketConnectionClosedException("Feed closed")
                    last_frame = time.monotonic()
                    received = datetime.now(timezone.utc).isoformat()
                    if not isinstance(frame, bytes) or len(frame) > 2_000_000:
                        raise ValueError("Unexpected socket frame")
                    message = msgpack.unpackb(frame, raw=False, strict_map_key=False)
                    if isinstance(message, list) and len(message) >= 4 and message[1] == "subscribed" and message[3] is None:
                        emit({"type": "connected", "receivedAt": received, "marketStateComplete": False})
                    records = state.apply(message, received)
                    if time.monotonic() - connected_at >= 30:
                        delay = 1
                    for record in records:
                        emit(record)
        except websocket.WebSocketBadStatusException as error:
            if error.status_code in (401, 403):
                raise FeedRejected(f"Socket access denied (HTTP {error.status_code}); stopping") from None
            if error.status_code in (429, 503):
                delay = retry_after_seconds(getattr(error, "resp_headers", None), max(60, delay))
            print(f"Socket HTTP {error.status_code}; reconnecting.", file=sys.stderr, flush=True)
        except (websocket.WebSocketException, OSError) as error:
            print(f"Socket disconnected ({type(error).__name__}); reconnecting.", file=sys.stderr, flush=True)
        remaining = deadline - time.monotonic()
        if remaining > 0 and not (stop_event and stop_event.is_set()):
            emit({"type": "disconnected", "receivedAt": datetime.now(timezone.utc).isoformat()})
            if stop_event:
                stop_event.wait(min(delay, remaining))
            else:
                time.sleep(min(delay, remaining))
            delay = min(delay * 2, 300)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--event-id", help="Only this DraftKings event ID (default: TT Elite league)")
    parser.add_argument("--duration", type=float, default=0, help="Capture seconds; 0 runs until Ctrl+C")
    parser.add_argument("--output", default="dk_live.jsonl", help="Append timestamped records to this JSONL file")
    args = parser.parse_args()
    if not math.isfinite(args.duration) or args.duration < 0:
        parser.error("--duration must be finite and nonnegative")
    if args.event_id and not re.fullmatch(r"[0-9]+", args.event_id):
        parser.error("--event-id must contain digits only")
    print(f"Read-only TT Elite stream -> {args.output}. Press Ctrl+C to stop.", flush=True)
    print("Incremental updates only; initial state is incomplete. Quote timestamps/status are logged.", flush=True)
    with open(args.output, "a", encoding="utf-8") as handle:
        def emit(record):
            line = json.dumps(record, ensure_ascii=True)
            handle.write(line + "\n")
            handle.flush()
            if record["type"] in ("score", "point10", "reset", "disconnected"):
                console = {k: v for k, v in record.items() if k != "markets"}
                print(json.dumps(console, ensure_ascii=True), flush=True)
        try:
            capture(args, emit)
        finally:
            emit({"type": "stopped", "receivedAt": datetime.now(timezone.utc).isoformat()})


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
    except (RuntimeError, ValueError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from None
