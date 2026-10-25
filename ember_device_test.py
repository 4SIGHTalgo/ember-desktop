"""One-opportunity, credential-free diagnostics for an explicitly started device test."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import time


class DeviceTest:
    FIELDS = {
        "api_authenticated", "feed_connected", "browser_started", "draftkings_signed_in",
        "location_state", "provider_can_bet", "native_wagering_grant", "stake_cap",
        "ev_percent", "ev_mode", "confidence", "cost", "signal_id", "event_id",
        "execution_status", "receipt_placed", "stake", "sizing", "error_type",
        "submission_dispatched", "submitted", "outcome", "complete", "opportunities",
        "phase", "error_code",
        "table_tennis_prepared", "retained_browser", "prepared_event_id", "session_renewal_verified",
        "observed_signals",
        "browser_state",
        "browser_closed",
        "browser_recoveries", "browser_recovery_reason",
        "max_bankroll_risk_percent", "kelly_multiplier", "conservative_level",
    }
    SIZING_FIELDS = {"availableBalance", "sizingMode", "kellyMultiplier", "sizingProbability",
                     "sizingDecimal", "fullKellyFraction", "bankrollFraction", "rawStake",
                     "stakeLimit", "stakeCapEnabled", "capped", "stake", "maxBankrollRiskPercent",
                     "bankrollRiskCap", "bankrollRiskCapped"}

    def __init__(self, directory, version, clock=time.monotonic, timeout=1200, readiness_only=False, scan_only=False):
        self.clock, self.timeout = clock, timeout
        self.deadline = None
        self.signal_id = None
        self.lock = threading.RLock()
        directory = Path(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.path = directory/f"device-test-{stamp}-{os.getpid()}.json"
        self.state = {"version": version, "mode": "scanner-navigation-readiness" if scan_only else "provider-login-readiness" if readiness_only else "one-opportunity-model-stake", "max_opportunities": 0 if readiness_only or scan_only else 1,
                      "configured_sizing": "selected-fractional-kelly", "authorized_ev_percent": 0,
                      "started_at": datetime.now(timezone.utc).isoformat(), "opportunities": 0,
                      "submitted": False, "complete": False, "outcome": "awaiting_start"}
        self.update()

    def update(self, **fields):
        if set(fields)-self.FIELDS:
            raise ValueError("Unexpected diagnostic fields; credentials must not enter reports")
        with self.lock:
            self.state.update(fields)
            self.state["updated_at"] = datetime.now(timezone.utc).isoformat()
            descriptor, temporary = tempfile.mkstemp(prefix=".device-test-", dir=self.path.parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                    json.dump(self.state, file, indent=2, allow_nan=False)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, self.path)
                self.path.chmod(0o600)
            finally:
                Path(temporary).unlink(missing_ok=True)

    def begin(self, stake_cap):
        with self.lock:
            if self.state["complete"] or self.deadline is not None:
                raise RuntimeError("Restart the application for another device test")
            self.deadline = self.clock()+self.timeout
            self.update(stake_cap=stake_cap, outcome="running")

    def permits(self, signal=None):
        with self.lock:
            return bool(self.deadline is not None and self.clock() < self.deadline
                        and not self.state["complete"]
                        and (self.signal_id is None or signal is None or signal["id"] == self.signal_id))

    def claim(self, signal):
        with self.lock:
            if self.state["max_opportunities"] == 0 or not self.permits(signal) or self.signal_id is not None:
                return False
            self.signal_id = signal["id"]
            self.update(opportunities=1, signal_id=signal["id"], event_id=signal["payload"]["event"]["eventId"])
            return True

    def policy(self, settings):
        if (settings.get("ev") != 0 or settings.get("ev_mode") != "conservative"
                or settings.get("confidence") not in (90, 95) or settings.get("cost") != 0):
            raise RuntimeError("The server strategy does not match the authorized conservative policy")
        self.update(ev_percent=settings["ev"], ev_mode=settings["ev_mode"],
                    confidence=settings["confidence"], cost=settings["cost"])

    def execution(self, result):
        result = result or {}
        sizing = {key: value for key, value in (result.get("sizing") or {}).items() if key in self.SIZING_FIELDS}
        status = result.get("status", "not_submitted")
        outcome = "uncertain_submission" if self.state.get("submission_dispatched") and self.state.get("submitted") is None else status
        self.update(execution_status=status, receipt_placed=bool(result.get("receiptPlaced")),
                    stake=result.get("stake"), sizing=sizing)
        self.finish(outcome)

    def finish(self, outcome):
        with self.lock:
            if not self.state["complete"]:
                self.update(outcome=outcome, complete=True)
