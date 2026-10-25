"""Versioned, data-only server/client messages and fail-closed local run leases."""
from datetime import datetime, timedelta
import math
import re
import threading
import time

from ember_opportunity import validate_opportunity
from ember_board import validate_board
from conservative_policy import contract_id
from execution_policy import allows_bet, allows_payload, ALLOWED_FAMILIES, DISTRIBUTION_FAMILIES, DISTRIBUTION_MARKETS
from tt_market_semantics import entry_window, name_key, score_state

VERSION = "0.2.3"
PROTOCOL = 1
PROTOCOL_V2 = 2

# The pre-open hint names the exact selection the scanner anticipates on the
# prepared surface. It carries identity only: no settings, no strategy context
# and no expiry authority. A validated signal remains the only thing that can
# authorize a stake or a submit; the hint can only pre-position a slip leg,
# which the submit path reuses only on an exact selection match.
STAGE_FIELDS = {"protocol", "event_id", "bet_type", "market_name", "market_id",
                "selection_id", "label", "line", "updated_at", "expires_at"}
STAGE_BET_TYPES = ("split", "complement", "closeout", "set_handicap", "point_handicap")


def validate_stage(value):
    """Strict wire check for the optional scanner pre-open hint."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) not in (STAGE_FIELDS, STAGE_FIELDS | {"event"}) or value["protocol"] != PROTOCOL:
        raise ValueError("Invalid pre-open hint")
    for field in ("event_id", "market_id", "selection_id"):
        item = value[field]
        if not isinstance(item, str) or not item.isascii() or not 0 < len(item) <= 64:
            raise ValueError("Invalid pre-open identity")
    if value["bet_type"] not in STAGE_BET_TYPES or value["bet_type"] not in ALLOWED_FAMILIES:
        raise ValueError("Invalid pre-open family")
    if not isinstance(value["market_name"], str) or not 0 < len(value["market_name"]) <= 80:
        raise ValueError("Invalid pre-open market")
    if not isinstance(value["label"], str) or not 0 < len(value["label"]) <= 200:
        raise ValueError("Invalid pre-open selection")
    line = value["line"]
    if line is not None and (type(line) not in (int, float) or not math.isfinite(line)
                             or abs(line) > 1e6):
        raise ValueError("Invalid pre-open line")
    updated = datetime.fromisoformat(value["updated_at"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(value["expires_at"].replace("Z", "+00:00"))
    if updated.tzinfo is None or expires.tzinfo is None or not timedelta(0) < expires-updated <= timedelta(seconds=15):
        raise ValueError("Invalid pre-open lifetime")
    if "event" in value:
        event = value["event"]
        if (not isinstance(event, dict) or event.get("eventId") != value["event_id"]
                or event.get("status") != "STARTED"
                or any(not isinstance(event.get(k), str) or not 0 < len(event[k]) <= 200 for k in ("player1", "player2"))
                or value["label"] not in (event["player1"], event["player2"])):
            raise ValueError("Invalid staged event identity")
        target = int(value["market_name"][-1]) if value["market_name"] in {"Game 2", "Game 3", "Game 4", "Game 5"} else 5
        if not allows_bet({"betType": value["bet_type"], "nextSet": target, "specification": {"line": line}},
                          {"name": value["market_name"]}, {"points": line}):
            raise ValueError("Disabled staged market")
    return value


def validate_preparation(value):
    if not isinstance(value, dict) or set(value) != {"protocol", "event", "market_hint", "expires_at"} or value["protocol"] != PROTOCOL:
        raise ValueError("Invalid browser preparation data")
    event, hint = value["event"], value["market_hint"]
    if not isinstance(event.get("eventId"), str) or not event["eventId"].isascii() or not event["eventId"].isdigit():
        raise ValueError("Invalid preparation event")
    if set(hint) != {"betType"} or hint["betType"] not in {"split", "closeout"}:
        raise ValueError("Preparation cannot contain a wager or executable command")
    if not all(isinstance(event.get(key), str) and 0 < len(event[key]) <= 200 for key in ("player1", "player2")):
        raise ValueError("Invalid preparation participants")
    timestamp(value["expires_at"])
    return value


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def validate_scanner(value):
    """Non-actionable telemetry; malformed metrics never become execution data."""
    if value is None:
        return None
    if not isinstance(value, dict) or type(value.get("healthy")) is not bool:
        raise ValueError("Invalid scanner status")
    counts = value.get("counts", {})
    allowed = {"events", "markets", "histories", "dk_live", "ttt_live", "mapped", "ready", "qualified"}
    if not isinstance(counts, dict) or any(key not in allowed or type(number) is not int or not 0 <= number <= 1_000_000 for key, number in counts.items()):
        raise ValueError("Invalid scanner counts")
    result = {"healthy": value["healthy"], "counts": dict(counts)}
    for name, limit in (("detail", 200), ("connection", 40)):
        item = value.get(name, "")
        if not isinstance(item, str) or len(item) > limit:
            raise ValueError("Invalid scanner description")
        result[name] = item
    for name in ("updated_at", "last_score_at", "last_market_at", "last_history_at"):
        item = value.get(name)
        if item is not None:
            if not isinstance(item, str) or len(item) > 64:
                raise ValueError("Invalid scanner timestamp")
            timestamp(item)
        result[name] = item
    try:
        result["opportunity"] = validate_opportunity(value.get("opportunity"))
    except (ValueError, TypeError, KeyError, OverflowError):
        result["opportunity"] = None
    # Older clients rebuild this dict from known fields and simply drop the
    # hint; newer clients validate it strictly.
    try:
        result["stage"] = validate_stage(value.get("stage"))
    except (ValueError, TypeError, KeyError, OverflowError):
        result["stage"] = None
    try:
        stages = value.get("stages", [])
        if not isinstance(stages, list) or len(stages) > 128:
            raise ValueError("Invalid stage catalog")
        result["stages"] = [validate_stage(stage) for stage in stages]
        if any(not stage or "event" not in stage for stage in result["stages"]):
            raise ValueError("Catalog needs event identity")
    except (ValueError, TypeError, KeyError, OverflowError):
        result["stages"] = []
    try:
        result["board"] = validate_board(value.get("board"))
    except (ValueError, TypeError, KeyError, OverflowError):
        result["board"] = None
    return result


PLAN_FIELDS = {"betType", "selection", "selectionId", "scoreState", "nextSet",
               "specification", "min_decimal", "stake_cents", "deadline", "display"}


def validate_plan(plan):
    """Strict wire check for the server-computed execution plan (protocol 2).

    The plan carries identity, execution floors and a display only. Any
    strategy data — sample counts, history availability, probabilities —
    makes it invalid: the client must execute the plan, never re-derive it.
    """
    if not isinstance(plan, dict) or set(plan) != PLAN_FIELDS:
        raise ValueError("Invalid execution plan")
    if plan["betType"] not in ALLOWED_FAMILIES:
        raise ValueError("Disabled market family")
    if not isinstance(plan["selection"], str) or not 0 < len(plan["selection"]) <= 200:
        raise ValueError("Invalid plan selection")
    if not isinstance(plan["selectionId"], str) or not re.fullmatch(r"[A-Za-z0-9_#:.+\-]{1,200}", plan["selectionId"]):
        raise ValueError("Invalid selection identifier")
    target = plan["nextSet"]
    if type(target) is not int or target not in ((1, 2, 3, 4, 5) if plan["betType"] in DISTRIBUTION_FAMILIES else (2, 3, 4, 5)):
        raise ValueError("Invalid plan target")
    state = plan["scoreState"]
    if not isinstance(state, list) or len(state) != 2 or any(type(value) is not int for value in state):
        raise ValueError("Invalid plan score state")
    specification = plan["specification"]
    if (not isinstance(specification, dict) or not set(specification) <= {"family", "player", "target", "line", "score"}
            or specification.get("family") != plan["betType"]
            or type(specification.get("player")) is not int or specification["player"] not in (0, 1)):
        raise ValueError("Invalid plan specification")
    if plan["betType"] in DISTRIBUTION_FAMILIES:
        if type(specification.get("line")) not in (int, float) or not math.isfinite(specification["line"]):
            raise ValueError("Invalid plan specification")
    elif type(specification.get("target")) is not int or specification["target"] not in (1, 2, 3, 4, 5):
        raise ValueError("Invalid plan specification")
    floor_price = plan["min_decimal"]
    if type(floor_price) not in (int, float) or not math.isfinite(floor_price) or floor_price <= 1:
        raise ValueError("Invalid price floor")
    stake = plan["stake_cents"]
    if type(stake) is not int or not 1 <= stake <= 10_000_000:
        raise ValueError("Invalid authorized stake")
    timestamp(plan["deadline"])
    display = plan["display"]
    if not isinstance(display, dict) or len(display) > 8:
        raise ValueError("Invalid plan display")
    for name, value in display.items():
        if not isinstance(name, str) or len(name) > 40 or not (
                value is None or isinstance(value, str) and len(value) <= 200
                or type(value) in (int, float) and math.isfinite(value)):
            raise ValueError("Invalid plan display")
    return plan


def validate_signal(signal):
    if (not isinstance(signal, dict) or signal.get("protocol") not in (PROTOCOL, PROTOCOL_V2)
            or type(signal.get("id")) is not int):
        raise ValueError("Unsupported signal envelope")
    if not isinstance(signal.get("contract"), str) or len(signal["contract"]) > 200:
        raise ValueError("Invalid contract")
    timestamp(signal["expires_at"])
    if signal["protocol"] == PROTOCOL_V2:
        payload = signal["payload"]
        if set(payload) != {"event", "market", "plan"}:
            raise ValueError("Only the server plan and its market surface are accepted")
        plan = validate_plan(payload["plan"])
        if timestamp(plan["deadline"]) > timestamp(signal["expires_at"]):
            raise ValueError("Plan deadline outlives the signal")
        event, market = payload["event"], payload["market"]
        for value in (event.get("eventId"), market.get("eventId"), market.get("marketId")):
            if not isinstance(value, str) or not value.isascii() or not value.isdigit():
                raise ValueError("Invalid provider identifier")
        if (event["eventId"] != market["eventId"]
                or market.get("name") not in {"Game 2", "Game 3", "Game 4", "Game 5", "Moneyline"} | set(DISTRIBUTION_MARKETS)):
            raise ValueError("Unsupported market")
        if not all(isinstance(event.get(key), str) and 0 < len(event[key]) <= 200 for key in ("player1", "player2")):
            raise ValueError("Invalid participants")
        matches = [outcome for outcome in (market.get("outcomes") or [])
                   if outcome.get("selectionId") == plan["selectionId"]]
        if len(matches) != 1 or name_key(matches[0].get("label", "")) != name_key(plan["selection"]):
            raise ValueError("Plan selection does not match a unique market outcome")
        if (not allows_bet(plan, market, matches[0]) or signal["contract"] != contract_id(event, plan)
                or plan["scoreState"] != score_state(event) or not entry_window(event)):
            raise ValueError("Signal does not match its contract or entry window")
        return signal
    # Protocol 1 strategy payloads are private-engine data; this client executes
    # server plans only, so they fail closed at the schema edge.
    raise ValueError("Unsupported signal envelope")

class RunLease:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.RLock()
        self.until = 0
        self.server_offset = 0
        self.running = False

    def update(self, message):
        with self.lock:
            enabled = message.get("enabled") and message.get("client_supported") and message.get("feed_healthy", True)
            server_now = timestamp(message["server_time"])
            self.server_offset = server_now - time.time()
            remaining = min(float(message.get("lease_seconds", 0)), timestamp(message["access_until"]) - server_now) if enabled else 0
            self.until = self.clock() + max(0, min(remaining, 5))

    def revoke(self):
        with self.lock:
            self.until = 0

    def permits(self, signal):
        with self.lock:
            return bool(self.running and self.clock() < self.until
                        and time.time() + self.server_offset < timestamp(signal["expires_at"]))
