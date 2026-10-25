"""Durable, account-scoped opportunity dispositions and a revisioned sync outbox."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import threading
import uuid


TERMINAL = {"missed", "placed", "uncertain", "handled", "observed_only"}
REASONS = {
    "waiting": "Waiting for execution",
    "browser_not_ready": "DraftKings browser was not ready",
    "login_required": "DraftKings sign-in was required",
    "location_required": "DraftKings wagering/location permission was pending",
    "session_stopped": "The local session was stopped",
    "read_only": "Observed in a read-only diagnostic session",
    "connection_lost": "Signal connection was interrupted",
    "lease_expired": "The execution lease was not current",
    "signal_expired": "The qualifying signal expired before submission",
    "superseded": "Another opportunity replaced this queued opportunity",
    "authorization_denied": "The server no longer authorized this signal",
    "claimed_elsewhere": "The contract was claimed by another client",
    "already_attempted": "The contract already has an execution reservation",
    "event_unavailable": "The matching event page did not become ready",
    "market_unavailable": "The matching market did not become ready",
    "entry_closed": "The start-state entry window closed",
    "odds_below_ev": "The final price no longer met the point-EV gate",
    "balance_unavailable": "Available cash was missing or insufficient",
    "sizing_failed": "The stake could not be sized or armed",
    "selection_failed": "The selection did not reach a ready bet slip",
    "execution_error": "Execution failed before a submit command was dispatched",
    "submission_uncertain": "Submission may have occurred; check My Bets",
    "app_restarted": "The app restarted before execution completed",
    "app_restarted_in_flight": "The app restarted with an unresolved execution; check My Bets",
    "placed": "A placed receipt was confirmed",
}
WIRE_FIELDS = ("id", "revision", "signal_id", "observed_at", "last_observed_at", "expires_at",
               "resolved_at", "outcome", "reason", "final_decimal_odds", "final_point_ev", "readiness")
READINESS_REASONS = {"ready", "event route not ready", "event identity not ready", "scoreboard identity",
    "market tab not selected", "winner section missing", "winner section ambiguous", "expanding winner section",
    "market section missing", "market section ambiguous", "expanding market section",
    "selection IDs missing or ambiguous", "selections are not in one market row", "game number mismatch",
    "selections disabled, hidden, already selected, or names/prices invalid", "page changed or data expired",
    "waiting for the exact market tab", "market surface not ready"}


def readiness_details(value):
    if not isinstance(value, dict):
        return {}
    status, reason = value.get("status"), value.get("reason")
    if status == "revealed" and reason is None:
        reason = "ready"
    return {"status": status if isinstance(status, str) and status in {"revealed", "waiting", "expired", "not_ready", "wrong event"} else "not_ready",
        "reason": reason if isinstance(reason, str) and reason in READINESS_REASONS else "market surface not ready",
        "attempts": min(1000, max(0, value.get("attempts", 0))) if type(value.get("attempts", 0)) is int else 0,
        "wait_ms": min(60000, max(0, value.get("wait_ms", 0))) if type(value.get("wait_ms", 0)) in (int, float) and math.isfinite(value.get("wait_ms", 0)) else 0}


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def not_before(value, *earlier):
    # Lease-based clock offsets can move by milliseconds between observations.
    return max(datetime.fromisoformat(item.replace("Z", "+00:00")) for item in (value, *earlier)).isoformat()


def finite(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def offer(signal):
    payload = signal["payload"]
    event = payload["event"]
    if "plan" in payload:  # Protocol 2: identity and floors only; the plan holds no strategy to replay.
        market, plan = payload["market"], payload["plan"]
        key = signal["contract"]+":"+",".join(str(value) for value in plan["scoreState"])
        matches = [outcome for outcome in (market.get("outcomes") or [])
                   if outcome.get("selectionId") == plan["selectionId"]]
        return key, {"event_id": event["eventId"], "players": [event["player1"], event["player2"]],
            "market": market.get("name"), "selection": plan["selection"],
            "decimal_odds": finite(matches[0].get("decimal")) if len(matches) == 1 else None,
            "point_ev": None, "minimum_ev": None, "ev_mode": None,
            "score_state": plan["scoreState"], "historical_meetings": None,
            "bet_type": plan["betType"], "specification": plan["specification"], "target_game": plan["nextSet"]}
    context, outcome, settings = (payload[key] for key in ("context", "outcome", "settings"))
    decimal = outcome["decimal"]
    try:
        american = float(outcome.get("american", ""))
        if math.isfinite(american) and abs(american) >= 100:
            decimal = 1 + (100/-american if american < 0 else american/100)
    except (TypeError, ValueError):
        pass
    count, wins = context.get("conditioningCount"), context.get("successCount", context.get("splitCount"))
    probability = wins/count if type(count) is int and type(wins) is int and 0 <= wins <= count and count > 0 else None
    point = 100*(probability*decimal-1-settings.get("cost", 0)/100) if probability is not None else None
    score = context.get("scoreState") or event.get("setTally") or [context.get("nextSet")]
    key = signal["contract"]+":"+",".join(str(value) for value in score)
    return key, {"event_id": event["eventId"], "players": [event["player1"], event["player2"]],
        "market": payload["market"]["name"], "selection": context.get("selection", outcome.get("label", "")),
        "decimal_odds": decimal, "point_ev": finite(point), "minimum_ev": settings["ev"],
        "ev_mode": settings["ev_mode"], "score_state": score,
        "historical_meetings": context.get("historicalMeetings")}


def result_reason(result):
    text = str((result or {}).get("reason", "")).lower()
    if any(word in text for word in ("ev minimum", "ev gate", "price moved", "price no longer", "price floor")):
        return "odds_below_ev"
    if any(word in text for word in ("entry window", "score or", "start-state", "entry changed")):
        return "entry_closed"
    if any(word in text for word in ("cash", "balance", "funds")):
        return "balance_unavailable"
    if any(word in text for word in ("kelly", "sizing", "stake")):
        return "sizing_failed"
    if any(word in text for word in ("sign in", "sign-in", "login", "log in")):
        return "login_required"
    if any(word in text for word in ("location", "geolocation")):
        return "location_required"
    if any(word in text for word in ("selection", "slip", "arming")):
        return "selection_failed"
    return "execution_error"


def reservations(path, account):
    if not Path(path).exists():
        return {}
    connection = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=1)
    try:
        return dict(connection.execute("SELECT contract,status FROM attempts WHERE account=?", (str(account),)))
    finally:
        connection.close()


class OpportunityJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        with self.db(write=False) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""CREATE TABLE IF NOT EXISTS opportunities (
                id TEXT PRIMARY KEY, origin TEXT NOT NULL, account TEXT NOT NULL,
                opportunity_key TEXT NOT NULL, contract TEXT NOT NULL,
                report TEXT NOT NULL, revision INTEGER NOT NULL, synced_revision INTEGER NOT NULL DEFAULT 0,
                outcome TEXT NOT NULL, expires_at TEXT NOT NULL,
                UNIQUE(origin, account, opportunity_key))""")
            connection.execute("CREATE INDEX IF NOT EXISTS opportunity_scope ON opportunities(origin,account,outcome)")

    @contextmanager
    def db(self, write=True):
        with self.lock:
            connection = sqlite3.connect(self.path, timeout=1)
            connection.row_factory = sqlite3.Row
            try:
                if write:
                    connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    @staticmethod
    def scoped(scope):
        return scope[0].rstrip("/"), str(scope[1])

    def _save(self, connection, row, report):
        report["revision"] = row["revision"]+1
        connection.execute("UPDATE opportunities SET report=?,revision=?,outcome=?,expires_at=? WHERE id=?",
            (json.dumps(report, allow_nan=False), report["revision"], report["outcome"], report["expires_at"], row["id"]))

    def observe(self, scope, signal, reason="waiting", now=None, reserved=False):
        now = now or utcnow()
        key, snapshot = offer(signal)
        origin, account = self.scoped(scope)
        with self.db() as connection:
            row = connection.execute("SELECT * FROM opportunities WHERE origin=? AND account=? AND opportunity_key=?",
                                     (origin, account, key)).fetchone()
            if row:
                report = json.loads(row["report"])
                if row["outcome"] in {"placed", "uncertain", "handled", "observed_only", "processing", "submitting"} or (reserved and row["outcome"] == "missed"):
                    return report["id"]
                if signal["id"] <= report["signal_id"]:
                    return report["id"]
                now = not_before(now, report["last_observed_at"])
                report.update(signal_id=signal["id"], snapshot=snapshot, last_observed_at=now,
                    expires_at=signal["expires_at"], outcome="pending", reason=reason, resolved_at=None)
                self._save(connection, row, report)
                return report["id"]
            identifier = str(uuid.uuid4())
            report = {"id": identifier, "revision": 1, "signal_id": signal["id"], "contract": signal["contract"],
                "observed_at": now, "last_observed_at": now, "expires_at": signal["expires_at"], "resolved_at": None,
                "outcome": "pending", "reason": reason, "snapshot": snapshot,
                "final_decimal_odds": None, "final_point_ev": None}
            connection.execute("INSERT INTO opportunities(id,origin,account,opportunity_key,contract,report,revision,outcome,expires_at) VALUES(?,?,?,?,?,?,1,?,?)",
                (identifier, origin, account, key, signal["contract"], json.dumps(report, allow_nan=False), "pending", signal["expires_at"]))
            return identifier

    def transition(self, scope, signal, outcome, reason, now=None, result=None):
        if reason not in REASONS or outcome not in TERMINAL | {"pending", "processing", "submitting"}:
            raise ValueError("Invalid opportunity disposition")
        key, snapshot = offer(signal)
        origin, account = self.scoped(scope)
        with self.db() as connection:
            row = connection.execute("SELECT * FROM opportunities WHERE origin=? AND account=? AND opportunity_key=?",
                                     (origin, account, key)).fetchone()
            if row is None:
                return False
            report = json.loads(row["report"])
            if row["outcome"] == "missed" and outcome in {"handled", "missed"} and reason == "already_attempted":
                return False
            if row["outcome"] in {"placed", "handled"} or (row["outcome"] == "observed_only" and outcome not in {"processing", "submitting", "placed"}) or (
                    row["outcome"] == "uncertain" and outcome != "placed" and not (result and result.get("submitted") is False)):
                return False
            if outcome == "missed" and row["outcome"] == "submitting":
                outcome, reason = "uncertain", "submission_uncertain"
            report.update(outcome=outcome, reason=reason,
                resolved_at=not_before(now or utcnow(), report["observed_at"], report["last_observed_at"]) if outcome in TERMINAL else None)
            if outcome == "processing":
                report.update(snapshot=snapshot, signal_id=signal["id"], expires_at=signal['expires_at'])
            if result:
                if result.get("readiness"):
                    report["readiness"] = readiness_details(result["readiness"])
                comparison = result.get("comparison") or result
                report["final_point_ev"] = finite(comparison.get("estimatedNetEVPercent"))
                report["final_decimal_odds"] = finite(comparison.get("slipDecimal", result.get("acceptedDecimal")))
                # An explicit non-submission result is stronger evidence than the
                # conservative pre-dispatch marker used for crash recovery.
                if result.get("submitted") is False:
                    report.update(outcome="missed", reason=result_reason(result))
            self._save(connection, row, report)
            return True

    def close_pending(self, scope, reason=None, now=None, expired_only=False, ledger_path=None):
        origin, account = self.scoped(scope)
        now = now or utcnow()
        attempts = reservations(ledger_path, account) if ledger_path else {}
        with self.db() as connection:
            rows = connection.execute("SELECT * FROM opportunities WHERE origin=? AND account=? AND outcome='pending'",
                                      (origin, account)).fetchall()
            for row in rows:
                report = json.loads(row["report"])
                if expired_only and datetime.fromisoformat(report["expires_at"].replace("Z", "+00:00")) > datetime.fromisoformat(now):
                    continue
                status = attempts.get(row["contract"])
                state = "handled" if status == "placed" else "uncertain" if status in {"reserved_or_unconfirmed", "unconfirmed", "stopped"} else "missed"
                disposition = "already_attempted" if state == "handled" else "submission_uncertain" if state == "uncertain" else reason or (report["reason"] if report["reason"] != "waiting" else "signal_expired")
                report.update(outcome=state, reason=disposition, resolved_at=not_before(now, report["last_observed_at"]))
                self._save(connection, row, report)

    def recover(self, scope, ledger_path):
        origin, account = self.scoped(scope)
        attempts = reservations(ledger_path, account)
        with self.db() as connection:
            rows = connection.execute("SELECT * FROM opportunities WHERE origin=? AND account=? AND outcome IN ('pending','processing','submitting')",
                                      (origin, account)).fetchall()
            for row in rows:
                report = json.loads(row["report"])
                status = attempts.get(row["contract"])
                if status == "placed":
                    state, reason = "handled", "already_attempted"
                elif row["outcome"] in {"processing", "submitting"} or status in {"reserved_or_unconfirmed", "unconfirmed", "stopped"}:
                    state, reason = "uncertain", "app_restarted_in_flight"
                else:
                    state, reason = "missed", "app_restarted"
                report.update(outcome=state, reason=reason, resolved_at=not_before(utcnow(), report["last_observed_at"]))
                self._save(connection, row, report)

    def pending_batch(self, scope, limit=20):
        with self.db(write=False) as connection:
            rows = connection.execute("SELECT report FROM opportunities WHERE origin=? AND account=? AND revision>synced_revision AND outcome IN ('missed','placed','uncertain','handled','observed_only') ORDER BY rowid LIMIT ?",
                                      (*self.scoped(scope), min(limit, 20))).fetchall()
        batch = [{key: report.get(key, {}) if key == "readiness" else report[key] for key in WIRE_FIELDS}
                 for report in (json.loads(row[0]) for row in rows)]
        # Existing servers validate a fixed reason vocabulary. Keep descriptive
        # local diagnostics without requiring a server cutover for this client.
        for report in batch:
            readiness = report['readiness']
            if readiness.get('reason') in {'market section missing', 'market section ambiguous', 'expanding market section'}:
                readiness['reason'] = 'market surface not ready'
        return batch

    def acknowledge(self, scope, acknowledgements):
        with self.db() as connection:
            for ack in acknowledgements:
                if not isinstance(ack, dict) or not isinstance(ack.get("id"), str) or type(ack.get("revision")) is not int:
                    continue
                connection.execute("UPDATE opportunities SET synced_revision=MAX(synced_revision,?) WHERE origin=? AND account=? AND id=? AND revision>=?",
                    (ack["revision"], *self.scoped(scope), ack["id"], ack["revision"]))

    def snapshot(self, scope, limit=50, outcome=None, offset=0):
        if outcome is not None and outcome not in TERMINAL:
            raise ValueError("Invalid history filter")
        limit, offset = max(1, min(limit, 100)), max(0, min(offset, 1_000_000))
        clause = " AND outcome=?" if outcome else ""
        parameters = (*self.scoped(scope), outcome) if outcome else self.scoped(scope)
        with self.db(write=False) as connection:
            rows = connection.execute("SELECT report,synced_revision FROM opportunities WHERE origin=? AND account=?"+clause+" ORDER BY rowid DESC LIMIT ? OFFSET ?",
                                      (*parameters, limit, offset)).fetchall()
            total = connection.execute("SELECT COUNT(*) FROM opportunities WHERE origin=? AND account=?"+clause, parameters).fetchone()[0]
            counts = dict(connection.execute("SELECT outcome,COUNT(*) FROM opportunities WHERE origin=? AND account=? GROUP BY outcome", self.scoped(scope)))
            queued = connection.execute("SELECT COUNT(*) FROM opportunities WHERE origin=? AND account=? AND revision>synced_revision AND outcome IN ('missed','placed','uncertain','handled','observed_only')", self.scoped(scope)).fetchone()[0]
        reports = []
        for row in rows:
            report = json.loads(row["report"])
            report.update(synced=row["synced_revision"] >= report["revision"], reason_label=REASONS[report["reason"]])
            reports.append(report)
        return {"records": reports, "counts": counts, "queued": queued, "total": total, "offset": offset,
                "next_offset": offset+limit if offset+limit < total else None, "filter": outcome or "all"}
