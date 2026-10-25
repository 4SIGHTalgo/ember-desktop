"""Cross-platform desktop sidecar. Secrets stay in OS keychain/native process memory."""
import base64
from copy import deepcopy
from collections import deque
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import queue
import secrets
import sqlite3
import sys
import threading
import time
from urllib.parse import urlsplit
import webbrowser

import requests
import websocket

from ember_protocol import (RunLease, VERSION, timestamp, validate_preparation, validate_scanner,
                           validate_signal, PROTOCOL_V2)
from ember_device_test import DeviceTest
from ember_browser_session import validation_ticket
from ember_browser_lifecycle import (ActiveSessionPower, BrowserCancelled, LifecycleLog, OwnedBrowser,
                                     startup_stage)
from ember_history import OpportunityJournal, reservations, result_reason
from ember_staging import Staging
from ember_board import display_board
from tt_market_exec import read_plan_slip
from draftkings_location import LocationRecheck
from conservative_policy import (DEFAULT_LEVEL, DEFAULT_KELLY, DEFAULT_RISK_PERCENT,
                                  client_settings, validate_risk)

OUTPUT = sys.stdout
OUTPUT_LOCK = threading.Lock()

# Startup wait-stage codes (ember_browser_lifecycle.BrowserWaiting) to the exact
# user-facing failure text. Anything absent keeps the generic recovery message,
# so a new code can never be silently mislabeled as a known stage.
STARTUP_STAGE_MESSAGES = {
    "chrome_not_installed": "Google Chrome was not found on this computer. Install Google Chrome, then press Start again; nothing was opened or submitted.",
    "browser_profile_in_use": "The saved browser profile is in use by another Ember run. Stop the other run, then press Start again.",
    "browser_ownership_unavailable": "The saved browser ownership record could not be read safely. Press Stop, then Start to relaunch a fresh owned browser.",
    "browser_listener_unverified": "The owned Chrome debugging listener could not be verified on this machine. Retrying startup; no wager was sent.",
    "browser_endpoint_unavailable": "Chrome opened, but its local debugging endpoint never became reachable. Check that security software is not blocking Chrome. Retrying startup; nothing was submitted.",
    "browser_start_failed": "Chrome exited while starting. Check Chrome's health and free memory, then press Start again.",
    "browser_owner_changed": "Another process took over the saved browser profile. Press Stop, then Start to reopen the owned browser.",
}


def emit(kind, **fields):
    with OUTPUT_LOCK:
        OUTPUT.write(json.dumps({"kind": kind, **fields}, allow_nan=False) + "\n")
        OUTPUT.flush()


class QuietOutput(io.TextIOBase):
    def write(self, value):
        # Existing browser scripts print human diagnostics and receipt text. Do
        # not forward those potentially personal details through the GUI IPC.
        return len(value)

    def flush(self):
        pass


class Ledger:
    def __init__(self, path):
        self.connection = sqlite3.connect(path)
        self.connection.execute("CREATE TABLE IF NOT EXISTS attempts (account TEXT, contract TEXT, status TEXT, PRIMARY KEY(account, contract))")
        self.connection.execute("CREATE TABLE IF NOT EXISTS economic_attempts (account TEXT, event TEXT, contract TEXT, context TEXT, PRIMARY KEY(account,contract))")
        self.connection.commit()

    def reserve(self, account, contract):
        try:
            self.connection.execute("INSERT INTO attempts VALUES (?,?,?)", (account, contract, "reserved_or_unconfirmed"))
            self.connection.commit()  # Durable before any submission can happen.
            return True
        except sqlite3.IntegrityError:
            return False

    def finish(self, account, contract, status):
        self.connection.execute("UPDATE attempts SET status=? WHERE account=? AND contract=?", (status, account, contract))
        self.connection.commit()

    def reserve_signal(self, account, signal):
        from tt_market_semantics import completed_scores, payoff_key
        payload = signal['payload']
        event = payload['event']
        context = payload['context'] if 'context' in payload else payload['plan']
        prefix = completed_scores(event.get('sets'))
        key = payoff_key(context, prefix)
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            for (saved,) in self.connection.execute('SELECT context FROM economic_attempts WHERE account=? AND event=?',
                                                   (account, event['eventId'])):
                if key is not None and payoff_key(json.loads(saved), prefix) == key:
                    return False
            if self.connection.execute('SELECT 1 FROM attempts WHERE account=? AND contract=?', (account, signal['contract'])).fetchone():
                return False
            self.connection.execute('INSERT INTO attempts VALUES (?,?,?)', (account, signal['contract'], 'reserved_or_unconfirmed'))
            self.connection.execute('INSERT INTO economic_attempts VALUES (?,?,?,?)',
                (account, event['eventId'], signal['contract'], json.dumps(context, allow_nan=False)))
        return True


class SignalQueue(queue.Queue):
    """One latest quote per waiting contract, with FIFO turns across contracts."""

    def put_latest(self, signal):
        with self.not_full:
            for index, pending in enumerate(self.queue):
                if pending['contract'] == signal['contract']:
                    if signal['id'] >= pending['id']:
                        self.queue[index] = signal
                    return
            if self.maxsize > 0 and self._qsize() >= self.maxsize:
                raise queue.Full
            self._put(signal)
            self.unfinished_tasks += 1
            self.not_empty.notify()


class Client:
    def __init__(self):
        import keyring
        self.keyring = keyring
        self.api = ""
        self.access_token = None
        self.token_until = 0
        self.account = None
        self.stream_url = None
        self.last_entitlement = None
        self.last_feed = None
        self.stream_connected = False
        self.scanner = None
        self.observed_signals = set()
        self.lease = RunLease()
        self.signals = SignalQueue(maxsize=100)
        self.pending_contracts = set()
        self.preparation = None
        self.staging = Staging()
        self.stop = threading.Event()
        self.run_cancel = threading.Event()
        self.run_generation = 0
        self.intent_lock = threading.RLock()
        self.auth_lock = threading.RLock()
        self.http_local = threading.local()
        self.login_lock = threading.Lock()
        self.run_lock = threading.Lock()
        self.provider_ready = threading.Event()
        self.driver = None
        self.bettor = None
        self.execution_thread = None
        self.stake = 0
        self.conservative_level = DEFAULT_LEVEL
        self.kelly_multiplier = DEFAULT_KELLY
        self.max_bankroll_risk_percent = DEFAULT_RISK_PERCENT
        self.browser_alerted = False
        self.browser_recovering = False
        self.resume_run = False
        self.browser_owner = None
        self.preserve_browser_on_exit = False
        self.data_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()/"Library/Application Support"))) / "Ember"
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.history = OpportunityJournal(self.data_dir/"opportunities.sqlite3")
        self.history_wake = threading.Event()
        self.history_force_sync = threading.Event()
        self.history_recovered = set()
        self.history_signature = None
        self.history_error = False
        self.history_filter = "missed"
        self.history_offset = 0
        self.provider_phase = "waiting"
        self.location_recheck = LocationRecheck()
        self.last_authorize_reason = "authorization_denied"
        timeout = int(os.environ.get("EMBER_DEVICE_TEST_TIMEOUT_SECONDS", "1200"))
        if not 120 <= timeout <= 7200:
            raise ValueError("Device test wait must be between two minutes and two hours")
        self.scan_only = os.environ.get("EMBER_DEVICE_TEST_SCAN_ONLY") == "1"
        self.readiness_only = os.environ.get("EMBER_DEVICE_TEST_LOGIN_ONLY") == "1" or self.scan_only
        self.device_test = DeviceTest(self.data_dir/"device-tests", VERSION, timeout=timeout,
                                      readiness_only=self.readiness_only, scan_only=self.scan_only) if os.environ.get("EMBER_DEVICE_TEST_ONCE") == "1" or self.readiness_only else None

    def permits(self, signal):
        return (not self.readiness_only and not self.run_cancel.is_set() and not self.stop.is_set()
                and self.provider_ready.is_set() and self.lease.permits(signal)
                and (not self.device_test or self.device_test.permits(signal)))

    def history_scope(self):
        with self.auth_lock:
            return (self.api.rstrip("/"), str(self.account)) if self.api and self.account is not None else None

    def history_call(self, action, *args, **kwargs):
        try:
            return action(*args, **kwargs)
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, OverflowError):
            if not self.history_error:
                emit("status", message="Opportunity history could not be saved or read. Check local storage; wager authorization is separate.")
            self.history_error = True
            return None

    def history_time(self):
        return (datetime.now(timezone.utc)+timedelta(seconds=self.lease.server_offset)).isoformat()

    def history_outcome(self, signal, outcome, reason, result=None):
        if outcome in {'placed', 'missed', 'handled', 'uncertain', 'observed_only'}:
            signal.setdefault('_timing', {}).setdefault('finished', time.monotonic())
        scope = signal.get("_history_scope") or self.history_scope()
        if scope:
            self.history_call(self.history.transition, scope, signal, outcome, reason, now=self.history_time(), result=result)
            self.history_wake.set()

    def receive_opportunity(self, signal, scope):
        signal.setdefault('_timing', {})['received'] = time.monotonic()
        signal["_history_scope"] = scope
        signal["_history_handled"] = False
        if not scope:
            return
        reason = ("session_stopped" if not self.lease.running else "browser_not_ready" if self.browser_recovering else "location_required" if self.provider_phase == "awaiting_location"
                  else "login_required" if not self.provider_ready.is_set() else "waiting")
        attempted = self.history_call(reservations, self.data_dir/"execution.sqlite3", scope[1]) or {}
        self.history_call(self.history.observe, scope, signal, reason, self.history_time(), reserved=signal["contract"] in attempted)
        if self.readiness_only:
            self.history_outcome(signal, "observed_only", "read_only")
        elif signal["contract"] in attempted:
            signal["_history_handled"] = True
            state = ("uncertain" if attempted[signal["contract"]] in {"reserved_or_unconfirmed", "unconfirmed", "stopped"}
                     else "handled" if attempted[signal["contract"]] == "placed" else "missed")
            self.history_outcome(signal, state, "submission_uncertain" if state == "uncertain" else "already_attempted")
        elif not self.lease.running:
            self.history_outcome(signal, "missed", "session_stopped")
        self.history_wake.set()

    def publish_history(self, sync_state="idle"):
        scope = self.history_scope()
        snapshot = self.history_call(self.history.snapshot, scope, outcome=None if self.history_filter == "all" else self.history_filter,
                                    offset=self.history_offset) if scope else {"records": [], "counts": {}, "queued": 0}
        if snapshot is None:
            snapshot = {"records": [], "counts": {}, "queued": 0}
        snapshot.update(sync_state="local_error" if self.history_error else sync_state)
        signature = json.dumps(snapshot, sort_keys=True)
        if signature != self.history_signature:
            self.history_signature = signature
            emit("missed_history", **snapshot)
        positions = []
        if scope:
            for outcome in ("placed", "uncertain"):
                page = self.history_call(self.history.snapshot, scope, outcome=outcome, limit=100)
                positions.extend((page or {}).get("records", []))
        emit("positions", records=positions)

    def sync_history_once(self):
        scope = self.history_scope()
        if not scope:
            return True
        self.history_call(self.history.close_pending, scope, now=self.history_time(), expired_only=True,
                          ledger_path=self.data_dir/"execution.sqlite3")
        batch = self.history_call(self.history.pending_batch, scope) or []
        if not batch:
            self.publish_history("idle")
            return True
        self.ensure_access()
        with self.auth_lock:
            if self.history_scope() != scope:
                return False
            api, token = self.api, self.access_token
        session = getattr(self.http_local, "session", None)
        if session is None:
            session = self.http_local.session = requests.Session()
        response = session.request("POST", api+"/api/v1/missed-bets/", json={"account_id": scope[1], "records": batch},
            headers={"Authorization": "Bearer "+token, "X-Ember-Version": VERSION}, timeout=8, allow_redirects=False)
        if response.status_code == 401:
            self.token_until = 0
        if response.status_code != 200:
            raise RuntimeError("history_sync_unavailable")
        acknowledgements = response.json().get("acknowledged", [])
        expected = {(row["id"], row["revision"]) for row in batch}
        valid = [ack for ack in acknowledgements if isinstance(ack, dict) and (ack.get("id"), ack.get("revision")) in expected]
        if {(ack["id"], ack["revision"]) for ack in valid} != expected:
            raise RuntimeError("history_sync_incomplete")
        self.history_call(self.history.acknowledge, scope, valid)
        self.publish_history("synced")
        return True

    def history_loop(self):
        retry = 2
        retry_at = 0
        while not self.stop.is_set():
            self.history_wake.clear()
            scope = self.history_scope()
            if scope:
                self.history_call(self.history.close_pending, scope, now=self.history_time(), expired_only=True,
                                  ledger_path=self.data_dir/"execution.sqlite3")
            try:
                if time.monotonic() >= retry_at or self.history_force_sync.is_set():
                    self.history_force_sync.clear()
                    success = self.sync_history_once()
                    retry = 2 if success else min(retry*2, 60)
                    retry_at = time.monotonic()+retry
                else:
                    self.publish_history("retrying" if retry > 2 else "idle")
            except Exception:
                retry = min(retry*2, 60)
                retry_at = time.monotonic()+retry
                self.publish_history("retrying")
            self.history_wake.wait(min(retry, 2))

    def stopped(self):
        complete = bool(self.device_test and self.device_test.state["complete"])
        emit("stopped", device_test_complete=complete,
              outcome=self.device_test.state["outcome"] if complete else None,
              browser_closed=self.browser_alerted, recoverable=self.preserve_browser_on_exit)

    def browser_closed(self):
        with self.intent_lock:
            if self.browser_alerted or not self.lease.running or self.stop.is_set():
                return False
            self.browser_alerted = True
            self.run_generation += 1
            self.lease.running = False
            self.run_cancel.set()
            self.provider_ready.clear()
            self.lease.revoke()
        scope = self.history_scope()
        if scope:
            self.history_call(self.history.close_pending, scope, "browser_not_ready", now=self.history_time(),
                              ledger_path=self.data_dir/"execution.sqlite3")
            self.history_wake.set()
        if self.device_test:
            self.device_test.update(browser_closed=True)
        emit("alert", code="browser_closed", message="DraftKings browser was closed, or its exit could not safely be identified as a crash. Monitoring stopped. Press Start to open it again.")
        return True

    def request_stop(self):
        with self.intent_lock:
            self.run_generation += 1
            self.run_cancel.set()
            self.lease.running = False
            self.provider_ready.clear()
            self.lease.revoke()
        scope = self.history_scope()
        if scope:
            self.history_call(self.history.close_pending, scope, "session_stopped", now=self.history_time(),
                              ledger_path=self.data_dir/"execution.sqlite3")
            self.history_wake.set()
        if not self.execution_thread or not self.execution_thread.is_alive():
            self.stopped()

    def pause_browser(self):
        self.provider_ready.clear()
        self.lease.revoke()
        self.provider_phase = "recovering"
        self.browser_recovering = True
        if self.device_test:
            self.device_test.update(phase="recovering_browser", browser_state="recovering",
                draftkings_signed_in=False, provider_can_bet=False, native_wagering_grant=False)
        while True:
            try:
                signal = self.signals.get_nowait()
                self.pending_contracts.discard(signal["contract"])
                self.history_outcome(signal, "missed", "browser_not_ready")
            except queue.Empty:
                break
        scope = self.history_scope()
        if scope:
            self.history_call(self.history.close_pending, scope, "browser_not_ready", now=self.history_time(),
                              ledger_path=self.data_dir/"execution.sqlite3")
            self.history_wake.set()


    def request(self, path, body=None, authenticated=False, timeout=15):
        started = time.monotonic()
        if authenticated:
            self.ensure_access(timeout=timeout)
        timeout -= time.monotonic()-started
        if timeout <= 0:
            raise RuntimeError('request_timeout')
        headers = {"X-Ember-Version": VERSION}
        if authenticated:
            headers["Authorization"] = "Bearer " + self.access_token
        session = getattr(self.http_local, "session", None)
        if session is None:
            session = self.http_local.session = requests.Session()
        response = session.request("POST" if body is not None else "GET", self.api+path, json=body,
                                   headers=headers, timeout=timeout, allow_redirects=False)
        if response.status_code >= 300:
            code = response.json().get("error", "request_failed") if "json" in response.headers.get("Content-Type", "") else "request_failed"
            raise RuntimeError(code)
        return response.json()

    def install_tokens(self, tokens):
        self.access_token = tokens["access_token"]
        self.token_until = time.monotonic()+tokens["expires_in"]-60
        self.keyring.set_password("Ember Desktop", self.api, tokens["refresh_token"])
        # The Rust host intercepts this message. It is never emitted to the UI.
        emit("credential", token=self.access_token)

    def ensure_access(self, timeout=15):
        if not self.api:
            raise RuntimeError("sign_in_required")
        started = time.monotonic()
        if not self.auth_lock.acquire(timeout=timeout):
            raise RuntimeError('request_timeout')
        try:
            if self.access_token and time.monotonic() < self.token_until:
                return
            refresh = self.keyring.get_password("Ember Desktop", self.api)
            if not refresh:
                raise RuntimeError("sign_in_required")
            tokens = self.request("/api/v1/token/refresh/", {"refresh_token": refresh},
                                  timeout=max(.001, timeout-(time.monotonic()-started)))
            self.install_tokens(tokens)
        finally:
            self.auth_lock.release()

    def login(self, api):
        if not self.login_lock.acquire(blocking=False):
            emit("status", message="Browser sign-in is already in progress; complete the open authorization request.")
            return
        try:
            return self._login(api)
        finally:
            self.login_lock.release()

    def _login(self, api):
        previous_scope = self.history_scope()
        parsed = urlsplit(api)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/") or parsed.username or parsed.query or parsed.fragment:
            raise ValueError("A public HTTPS origin is required")
        with self.run_lock:
            if self.lease.running:
                raise RuntimeError("stop_before_changing_account")
            with self.auth_lock:
                self.api = api.rstrip("/")
                self.access_token = None
                self.account = None
        try:
            self.ensure_access()
        except Exception:
            verifier = secrets.token_urlsafe(48)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            grant = self.request("/api/v1/device/start/", {"challenge": challenge, "label": "Ember " + sys.platform})
            if not grant["verification_uri"].startswith(self.api+"/desktop/authorize/"):
                raise RuntimeError("invalid_authorization_url")
            emit("status", message="Approve the browser sign-in. Code: " + grant["user_code"])
            webbrowser.open(grant["verification_uri"])
            deadline = time.monotonic()+grant["expires_in"]
            while not self.stop.wait(grant["interval"]) and time.monotonic() < deadline:
                try:
                    tokens = self.request("/api/v1/device/token/", {"device_code": grant["device_code"], "verifier": verifier})
                    with self.auth_lock:
                        self.install_tokens(tokens)
                    break
                except RuntimeError as error:
                    if str(error) != "authorization_pending":
                        raise
            else:
                raise RuntimeError("authorization_expired")
        status = self.request("/api/v1/status/", authenticated=True)
        with self.auth_lock:
            self.account = status["account_id"]
        scope = self.history_scope()
        if scope != previous_scope:
            self.history_offset = 0
        if scope not in self.history_recovered:
            self.history_call(self.history.recover, scope, self.data_dir/"execution.sqlite3")
            self.history_recovered.add(scope)
        self.publish_history()
        self.history_wake.set()
        if self.device_test:
            self.device_test.update(api_authenticated=True)
        self.stream_url = status["stream_url"]
        emit("status", message="Signed in. " + ("Subscription active." if status["enabled"] else "An active subscription is required."))
        emit("signed_in", enabled=status["enabled"], supported=status["client_supported"])

    def feed_disconnected(self, reason, scope=None):
        self.lease.revoke()
        self.preparation = None
        self.last_feed = None
        self.stream_connected = False
        scope = scope or self.history_scope()
        if scope:
            self.history_call(self.history.close_pending, scope, "connection_lost", now=self.history_time(),
                              ledger_path=self.data_dir/"execution.sqlite3")
            self.history_wake.set()
        while not self.signals.empty():
            try:
                self.signals.get_nowait()
            except queue.Empty:
                break
        self.pending_contracts.clear()
        emit("scanner", connected=False, healthy=False, reason=reason,
             received_at=datetime.now(timezone.utc).isoformat(), snapshot=self.scanner, server_offset=self.lease.server_offset)

    def accept_lease(self, message):
        self.lease.update(message)
        self.preparation = validate_preparation(message["preparation"]) if message.get("preparation") else None
        try:
            self.scanner = validate_scanner(message.get("scanner"))
        except (ValueError, TypeError):
            self.scanner = None
        healthy = bool(message.get("feed_healthy", False))
        changed = not self.stream_connected or healthy != self.last_feed
        self.stream_connected = True
        self.last_feed = healthy
        self.scanner = display_board(self.scanner, self.conservative_level)
        if self.device_test:
            self.device_test.update(feed_connected=healthy)
        emit("scanner", connected=True, healthy=healthy, reason="connected",
             received_at=datetime.now(timezone.utc).isoformat(), snapshot=self.scanner, server_offset=self.lease.server_offset)
        if changed:
            emit("status", message="Live signal feed connected." if healthy else "Live connection is open; waiting for fresh, verified source data.")

    def stream(self):
        while not self.stop.wait(.5):
            if not self.api or not self.access_token:
                continue
            connection = None
            stream_scope = self.history_scope()
            reason, retry_delay = "connection_lost", 3
            try:
                self.ensure_access()
                status = self.request("/api/v1/status/", authenticated=True)
                self.stream_url = status["stream_url"]
                state = (status["enabled"], status["client_supported"])
                if state != self.last_entitlement:
                    self.last_entitlement = state
                    emit("entitlement", enabled=state[0], supported=state[1])
                stream = urlsplit(self.stream_url)
                if stream.scheme != "wss" or not stream.hostname or stream.username or stream.query or stream.fragment or stream.path != "/api/v1/signals/stream/":
                    raise RuntimeError("invalid_stream_endpoint")
                connection = websocket.create_connection(self.stream_url, timeout=4)
                stream_token = self.access_token
                connection.send(json.dumps({"token": stream_token, "version": VERSION}))
                while not self.stop.is_set():
                    if self.history_scope() != stream_scope:
                        break
                    if self.access_token != stream_token or time.monotonic() >= self.token_until:
                        reason, retry_delay = "session_refresh", 0
                        self.feed_disconnected(reason)
                        emit("status", message="Refreshing Ember sign-in and reconnecting the live signal feed.")
                        self.ensure_access()
                        break
                    raw = connection.recv()
                    if not raw:
                        break
                    message = json.loads(raw)
                    if message.get("type") == "lease":
                        self.accept_lease(message)
                    elif message.get("type") == "revoked":
                        if self.access_token != stream_token or time.monotonic() >= self.token_until:
                            reason, retry_delay = "session_refresh", 0
                        else:
                            reason = "access_required"
                            self.token_until = 0
                            emit("status", message="Signal access paused; rechecking Ember sign-in and subscription.")
                        break
                    elif message.get("type") == "signal":
                        signal = validate_signal(message["signal"])
                        self.receive_opportunity(signal, stream_scope)
                        if self.scan_only and signal["id"] not in self.observed_signals:
                            self.observed_signals.add(signal["id"])
                            if self.device_test:
                                self.device_test.update(observed_signals=len(self.observed_signals), signal_id=signal["id"],
                                                        event_id=signal["payload"]["event"]["eventId"])
                            emit("status", message=f"Observed model signal {signal['id']} in scan-only mode; no wager will be submitted.")
                        elif self.run_permitted() and not signal.get("_history_handled"):
                            try:
                                self.signals.put_latest(signal)
                            except queue.Full:
                                self.history_outcome(signal, "missed", "superseded")
            except Exception as error:
                if isinstance(error, RuntimeError) and str(error) in {"unauthorized", "invalid_grant"}:
                    self.token_until = 0
                    reason = "sign_in_required"
                emit("status", message="Signal connection interrupted; execution is paused while Ember reconnects.")
            finally:
                self.feed_disconnected(reason, stream_scope)
                if connection:
                    try:
                        connection.close()
                    except Exception:
                        pass  # A failed close must not kill the reconnect thread.
            self.stop.wait(retry_delay)

    def run_permitted(self):
        return (not self.readiness_only and not self.run_cancel.is_set() and not self.stop.is_set()
                and self.provider_ready.is_set() and self.lease.running and self.lease.clock() < self.lease.until)

    def renew_signal(self, signal, status):
        """Renew time only on the same validated server contract, in place."""
        if 'signal' not in status:
            return
        renewed = validate_signal(status['signal'])
        def identity(value):
            value = deepcopy({key: value[key] for key in ('protocol', 'id', 'contract', 'payload')})
            payload = value['payload']
            if 'plan' in payload:
                payload['plan'].pop('deadline')
            for key in ('event', 'market'):
                payload[key].pop('receivedAt', None)
            return value
        if (identity(renewed) != identity(signal)
                or timestamp(renewed['expires_at']) < timestamp(signal['expires_at'])
                or ('plan' in signal['payload'] and timestamp(renewed['payload']['plan']['deadline']) <
                    timestamp(signal['payload']['plan']['deadline']))):
            raise ValueError('authorization_contract_changed')
        signal['expires_at'] = renewed['expires_at']
        if 'plan' in signal['payload']:
            signal['payload']['plan']['deadline'] = renewed['payload']['plan']['deadline']

    def authorization_timeout(self, signal, renewal=False):
        """Yield preclaim turns quickly; a reserved quote gets a longer reply budget."""
        left = self.lease.until-self.lease.clock()
        expiry = timestamp(signal['expires_at'])-time.time()-self.lease.server_offset
        plan = signal['payload'].get('plan')
        if plan:
            expiry = min(expiry, timestamp(plan['deadline'])-time.time()-self.lease.server_offset)
        return max(.001, min(.3 if renewal else .75, left, .3 if renewal and expiry <= 0 else expiry))

    def authorize(self, signal, claim=False):
        self.last_authorize_reason = "authorization_denied"
        if not self.run_permitted():
            self.last_authorize_reason = ("signal_expired" if timestamp(signal["expires_at"]) <= time.time()+self.lease.server_offset
                else "session_stopped" if not self.lease.running else "browser_not_ready" if not self.provider_ready.is_set() else "lease_expired")
            return False
        try:
            status = self.request(f"/api/v1/signals/{signal['id']}/authorize/", {"claim": claim}, authenticated=True,
                                  timeout=self.authorization_timeout(signal, renewal=True))
            self.lease.update(status)
            self.renew_signal(signal, status)
            permitted = self.permits(signal)
            if not permitted:
                self.last_authorize_reason = ("signal_expired" if timestamp(signal['expires_at']) <= time.time()+self.lease.server_offset
                    else "session_stopped" if not self.lease.running else "browser_not_ready" if not self.provider_ready.is_set() else "lease_expired")
            return permitted
        except Exception as error:
            if not claim and (isinstance(error, requests.Timeout) or str(error) == 'request_timeout'):
                self.last_authorize_reason = 'authorization_unavailable'
                return False
            if str(error) == "contract_claimed_by_another_client":
                self.last_authorize_reason = "claimed_elsewhere"
                return False
            elif str(error) == "signal_expired":
                self.last_authorize_reason = "signal_expired"
                return False
            self.lease.revoke()
            return False

    def claim_values(self, signal, ledger=None):
        """Claim one observed quote; only the server may size the stake."""

        def authorize_values(quote):
            available = (quote.get("balance") or {}).get("availableBalance")
            decimal = quote.get("decimalOdds")
            if (type(available) not in (int, float) or not math.isfinite(available) or available < 0
                    or type(decimal) not in (int, float) or not math.isfinite(decimal) or decimal <= 1):
                return {"denied": "sizing_failed"}
            self.last_authorize_reason = "authorization_denied"
            if (not self.permits(signal) or timestamp(signal['payload']['plan']['deadline']) <=
                    time.time()+self.lease.server_offset):
                return {"denied": "signal_expired"}
            if ledger is not None and not signal.get('_reserved'):
                if not ledger.reserve_signal(self.account, signal):
                    return {"denied": "already_attempted"}
                signal['_reserved'] = True
            try:
                status = self.request(f"/api/v1/signals/{signal['id']}/authorize/",
                                      {"claim": True, "observed": {"decimal": decimal,
                                                                   "balance_cents": int(round(available*100))},
                                       "sizing": {"kelly_multiplier": self.kelly_multiplier,
                                                  "max_bankroll_risk_percent": self.max_bankroll_risk_percent,
                                                  "conservative_level": self.conservative_level}},
                                      authenticated=True, timeout=self.authorization_timeout(signal))
            except Exception as error:
                if str(error) == "contract_claimed_by_another_client":
                    self.last_authorize_reason = "claimed_elsewhere"
                elif str(error) == "signal_expired":
                    self.last_authorize_reason = "signal_expired"
                elif str(error) == "price_below_floor":
                    self.last_authorize_reason = "odds_below_ev"
                elif str(error) == "sizing_failed":
                    self.last_authorize_reason = "sizing_failed"
                else:
                    self.lease.revoke()
                return {"denied": self.last_authorize_reason}
            self.lease.update(status)
            try:
                self.renew_signal(signal, status)
            except (ValueError, TypeError, KeyError, OverflowError):
                self.lease.revoke()
                return {"denied": "authorization_denied"}
            if not self.permits(signal):
                self.last_authorize_reason = ("signal_expired" if timestamp(signal['expires_at']) <= time.time()+self.lease.server_offset
                    else "session_stopped" if not self.lease.running else "browser_not_ready" if not self.provider_ready.is_set() else "lease_expired")
                return {"denied": self.last_authorize_reason}
            fields = status.get("plan")
            if (not isinstance(fields, dict) or set(fields) != {"stake_cents", "min_decimal", "echo"}
                    or type(fields.get("stake_cents")) is not int or not 1 <= fields["stake_cents"] <= 10_000_000
                    or type(fields.get("min_decimal")) not in (int, float) or not math.isfinite(fields["min_decimal"])
                    or fields["min_decimal"] <= 1
                    or not isinstance(fields.get("echo"), str) or not 0 < len(fields["echo"]) <= 200):
                return {"denied": "authorization_denied"}
            # The claim is the sole authority; the signal plan's floor and stake
            # were provisional display data at the 90% publication bound.
            return {"stake": fields["stake_cents"]/100, "min_decimal": fields["min_decimal"],
                    "echo": fields["echo"]}

        return authorize_values

    def defer_readiness(self, signal, readiness):
        """Yield only before claim/reservation/submission, within the original expiry."""
        if signal.get('_reserved') or not self.permits(signal) or min(self.lease.until-self.lease.clock(),
                timestamp(signal['expires_at'])-time.time()-self.lease.server_offset) <= .6:
            return False
        try:
            waiting = self.signals.empty()
            self.signals.put_latest(signal)
        except queue.Full:
            return False
        self.history_outcome(signal, 'pending', 'waiting', result={'readiness': readiness})
        if waiting:
            self.stop.wait(.03)
        return True

    def execute(self):
        owner, attempt = None, 0
        power = ActiveSessionPower()
        active = lambda: self.lease.running and not self.run_cancel.is_set() and not self.stop.is_set()
        try:
            owner = self.browser_owner = OwnedBrowser(self.data_dir, self.history_scope(),
                ticket=validation_ticket(self.data_dir), resume=self.resume_run)
            if not power.acquire():
                emit("status", message="System idle-sleep prevention is unavailable. Monitoring requires the computer to stay awake.")
            while active():
                if self.device_test and not self.device_test.permits():
                    self.device_test.finish("timed_out")
                    break
                started = time.monotonic()
                try:
                    self.execute_browser(owner, active)
                    break
                except BrowserCancelled:
                    break
                except Exception as error:
                    if not active():
                        break
                    self.pause_browser()
                    reason = owner.recovery(error)
                    if reason == "closed":
                        self.browser_closed()
                        break
                    attempt = 1 if time.monotonic()-started >= 60 else attempt+1
                    delay = min(2**min(attempt-1, 5), 30)
                    stage = startup_stage(error)
                    owner.log.record("recovery_wait", attempt=attempt, stage=stage)
                    if self.device_test:
                        self.device_test.update(browser_recoveries=attempt, browser_recovery_reason=reason)
                    message = STARTUP_STAGE_MESSAGES.get(stage) or (
                        "DraftKings connection interrupted. Wagering is paused; retrying the owned browser "
                        f"connection in {delay}s. Stop cancels recovery.")
                    emit("browser_recovery", state="recovering", reason=reason, stage=stage, message=message)
                    self.run_cancel.wait(delay)
        finally:
            close = self.run_cancel.is_set() or self.stop.is_set() or bool(self.device_test and self.device_test.state["complete"])
            self.provider_ready.clear()
            self.browser_recovering = False
            self.lease.running = False
            self.lease.revoke()
            scope = self.history_scope()
            if scope:
                self.history_call(self.history.close_pending, scope, "browser_not_ready" if self.browser_alerted else "session_stopped",
                    now=self.history_time(), ledger_path=self.data_dir/"execution.sqlite3")
                self.history_wake.set()
            if self.device_test:
                state = self.device_test.state
                outcome = ("scanner_navigation_ready_no_wagers" if self.scan_only and state.get("prepared_event_id") else
                    "scanner_standby_no_wagers" if self.scan_only and state.get("table_tennis_prepared") else
                    "login_ready_no_wagers" if self.readiness_only and state.get("phase") == "login_ready_no_wagers" else "stopped_before_execution")
                self.device_test.finish("browser_closed_no_wagers" if self.browser_alerted and self.readiness_only else "browser_closed" if self.browser_alerted else outcome)
                close = True
            try:
                if owner:
                    with redirect_stdout(QuietOutput()):
                        owner.release(close=close and not self.preserve_browser_on_exit)
            finally:
                self.browser_owner = None
                power.release()
                self.stopped()

    def maintain_staging(self, driver, core, browser_preparation, market_readiness, mono):
        stages = (self.scanner or {}).get('stages', [])
        result = self.staging.step(driver, core, market_readiness, stages,
            valid=lambda: self.run_permitted() and self.last_feed is True,
            server_now=time.time()+self.lease.server_offset)
        self.publish_slip(driver)
        return result

    def publish_slip(self, driver):
        snapshot = read_plan_slip(driver)
        if snapshot.get("status") not in ("empty", "present"):
            return
        stages = (self.scanner or {}).get("stages", [])
        legs = []
        for leg in snapshot.get("legs", []):
            hint = next((s for s in stages if (s["event_id"], s["market_id"], s["selection_id"]) ==
                         (leg["eventId"], leg["marketId"], leg["selectionId"])), {})
            legs.append({"event_id": leg["eventId"], "market_id": leg["marketId"],
                         "selection_id": leg["selectionId"], "label": hint.get("label", leg["text"]),
                         "market": hint.get("market_name", ""), "line": hint.get("line"),
                         "decimal_odds": leg.get("decimalOdds"), "status": "suspended" if leg.get("suspended") else "ready"})
        emit("slip", legs=legs, checked_at=self.history_time())

    def hold_preparation(self):
        scanner = self.scanner or {}
        return bool(self.staging.inventory or scanner.get('stages') or (scanner.get('board') or {}).get('events'))

    def execute_browser(self, owner, active):
        import tt_market_exec as core
        from draftkings_login import LOGIN_URL, allow_location_permissions, goto_tt_elite
        from ember_browser_preparation import BrowserPreparation
        from ember_market_readiness import MarketReadiness
        from ember_provider_login import ProviderLoginGate, browser_connection_lost
        from draftkings_session import SessionMaintenance
        core.ROOT = self.data_dir
        import tt_market_exec
        # The executor owns the slip capture path; the strategy module keeps its
        # own data-dir root for CLI-era reports.
        tt_market_exec.ROOT = self.data_dir
        tt_market_exec.SLIP_CAPTURE = self.data_dir / "last-slip.json"
        ledger = Ledger(self.data_dir / "execution.sqlite3")
        maintenance = SessionMaintenance(core.DEFAULT_KEEP_ALIVE)
        login_gate = ProviderLoginGate(recheck=self.location_recheck, active=active)
        self.provider_ready.clear()
        table_prepared = False
        last_provider = None
        def browser_status(**fields):
            emit("browser", **fields)
            if fields["state"] != "provider_wait" and fields.get("changed", True):
                emit("status", message=fields["text"])
            if self.device_test:
                self.device_test.update(browser_state=fields["state"])
                if fields["state"] == "watching_event":
                    self.device_test.update(prepared_event_id=fields["event_id"])
        market_readiness = MarketReadiness(core)
        browser_preparation = BrowserPreparation(core, login_gate.navigation_started, goto_tt_elite, browser_status,
                                                 market_readiness=market_readiness)
        original_execute = None
        current = None
        submission_may_have_run = False
        try:
            with redirect_stdout(QuietOutput()):
                if self.device_test:
                    self.device_test.update(phase="browser_launch")
                self.driver = owner.open(active)
                if self.device_test:
                    self.device_test.update(phase="browser_permissions")
                allow_location_permissions(self.driver)
                core.prepare_browser_execution(self.driver)
                if self.device_test:
                    self.device_test.update(phase="provider_navigation")
                if self.driver.current_url in {"", "about:blank", "chrome://newtab/"}:
                    self.driver.get(LOGIN_URL)
            if self.browser_recovering:
                emit("browser_recovery", state="connected", message="Owned browser reconnected. Rechecking DraftKings sign-in, location, and fresh signal permission before resuming.")
            self.browser_recovering = False
            emit("status", message="DraftKings uses Ember's saved local profile. Complete any sign-in, SMS, and location check; monitoring will continue automatically.")
            if self.device_test:
                self.device_test.update(browser_started=True, retained_browser=owner.retained, phase="awaiting_provider_signin")
            original_execute = self.driver.execute_script

            def guarded(script, *args):
                nonlocal submission_may_have_run
                if script == core.CONFIRM_PLAN_SCRIPT:
                    if current is None or not self.permits(current):
                        return {"status": "aborted", "reason": "Ember authorization expired"}
                    # A browser-side deadline also covers a delayed CDP command.
                    deadlines = [self.lease.until-time.monotonic(),
                                 timestamp(current["expires_at"])-time.time()-self.lease.server_offset]
                    plan = current.get("payload", {}).get("plan")
                    if plan:
                        # The server plan may expire before the signal's own window.
                        deadlines.append(timestamp(plan["deadline"])-time.time()-self.lease.server_offset)
                    remaining = min(deadlines)
                    script = f"if(Date.now()>{int(time.time()*1000+max(0,remaining)*1000)})return {{status:'aborted',reason:'Ember authorization expired'}};\n"+script
                    script = "const emberNative=window.__playSpan?.playSpan;if(window.__isLoggedIn!==true||window.__canBet!==true||!emberNative?.clientKey||emberNative.grantType!=='PlaySportsbook')return {status:'aborted',reason:'DraftKings sign-in or wagering permission expired'};\n"+script
                    if self.device_test:
                        self.device_test.update(submission_dispatched=True, submitted=None)
                    previous_submission = submission_may_have_run
                    submission_may_have_run = True
                    self.history_outcome(current, "submitting", "waiting")
                    current.setdefault('_timing', {})['submitted'] = time.monotonic()
                    response = original_execute(script, *args)
                    if (not previous_submission and isinstance(response, dict)
                            and response.get("status") in {"aborted", "resize", "skipped", "waiting"}):
                        submission_may_have_run = False
                        self.history_outcome(current, "processing", "waiting")
                    if self.device_test:
                        self.device_test.update(submitted=bool(response and response.get("status") == "submitted"))
                    return response
                return original_execute(script, *args)

            self.driver.execute_script = guarded
            while active():
                current = None
                submission_may_have_run = False
                if any(getattr(self.driver, flag, False) is True for flag in ('_tt_plan_uncertain', '_tt_plan_stopped')):
                    self.lease.running = False
                    break
                if self.device_test and not self.device_test.permits():
                    self.device_test.finish("timed_out")
                    break
                with redirect_stdout(QuietOutput()):
                    provider = login_gate.poll(self.driver, core.location_status)
                if not active():
                    break
                signed_in = bool(provider.get("signed_in"))
                location = provider["location"]
                evidence = {"signed_in": signed_in, "ready": bool(provider["ready"]),
                            "can_bet": bool(provider.get("provider_can_bet")), "phase": provider.get("phase", "waiting")}
                self.provider_phase = evidence["phase"]
                if evidence != last_provider:
                    last_provider = evidence
                    emit("provider", **evidence)
                if provider.get("message"):
                    emit("status", message=provider["message"])
                if self.device_test:
                    evidence = {"draftkings_signed_in": signed_in, "location_state": location.get("state", "unknown"),
                                "provider_can_bet": bool(provider.get("provider_can_bet")), "native_wagering_grant": bool(provider.get("native_wagering_grant"))}
                    if any(self.device_test.state.get(key) != value for key, value in evidence.items()):
                        self.device_test.update(**evidence)
                        emit("status", message=f"Device check: sign-in {'confirmed' if signed_in else 'pending'}; location {location.get('state','unknown')}; provider can-bet {bool(provider.get('provider_can_bet'))}.")
                if not provider["ready"]:
                    self.provider_ready.clear()
                    if self.device_test:
                        phase = "awaiting_provider_location" if signed_in else "awaiting_provider_signin"
                        if self.device_test.state.get("phase") != phase:
                            self.device_test.update(phase=phase)
                    if provider["signed_out_confirmed"]:
                        table_prepared = False
                    if provider.get("message"):
                        browser_preparation.publish("provider_wait", provider["message"])
                    elif browser_preparation.last_status and browser_preparation.last_status[0] == "provider_wait":
                        browser_preparation.publish("provider_wait", browser_preparation.last_status[1])
                    self.stop.wait(.5)
                    continue
                if not table_prepared:
                    self.provider_ready.clear()
                    if not market_readiness.provider_ready(self.driver):
                        browser_preparation.publish('provider_loading',market_readiness.error_reason)
                        self.stop.wait(.5)
                        continue
                    snapshot = read_plan_slip(self.driver)
                    if snapshot.get('status') == 'present' and snapshot.get('legs'):
                        self.staging.inventory = snapshot['legs']
                        table_prepared = True
                        continue
                    try:
                        with redirect_stdout(QuietOutput()):
                            login_gate.navigation_started()
                            goto_tt_elite(self.driver)
                    except core.FATAL_BROWSER_ERRORS:
                        raise
                    except (RuntimeError, core.WebDriverException):
                        emit("status", message="Waiting for the TT Elite league match list; complete any visible DraftKings sign-in or location prompt.")
                        self.stop.wait(2)
                        continue
                    table_prepared = True
                    if self.device_test:
                        self.device_test.update(table_tennis_prepared=True)
                    emit("status", message="TT Elite league match list opened. Rechecking the signed-in session before enabling model signals.")
                    continue
                if self.readiness_only and not self.scan_only:
                    if self.device_test and self.device_test.state.get("phase") != "login_ready_no_wagers":
                        self.device_test.update(phase="login_ready_no_wagers")
                        emit("status", message="Login-only test passed: DraftKings is signed in on Table Tennis. Wager execution is disabled for this test.")
                    self.stop.wait(.5)
                    continue
                if not self.provider_ready.is_set() and not self.scan_only:
                    emit("status", message="Table Tennis is signed in and ready. Waiting for fresh, qualifying model signals.")
                if not self.scan_only:
                    self.provider_ready.set()
                with redirect_stdout(QuietOutput()):
                    maintenance.observe_network(self.driver, time.monotonic())
                    maintenance.activity(self.driver, time.monotonic(), "signed-in", busy=not self.signals.empty(),
                                         blocked=bool(location.get("blocked")))
                if self.device_test and maintenance.report.get("lastVerificationAt") and not self.device_test.state.get("session_renewal_verified"):
                    self.device_test.update(session_renewal_verified=True)
                if self.device_test and self.device_test.state.get("phase") in {"awaiting_provider_signin", "awaiting_provider_location"}:
                    self.device_test.update(phase="waiting_for_qualified_signal")
                if self.scan_only:
                    browser_preparation.step(self.driver, self.preparation,
                        self.lease.clock() < self.lease.until and self.last_feed is True,
                        time.time()+self.lease.server_offset)
                    if self.device_test:
                        self.device_test.update(phase="watching_verified_event_no_wagers" if browser_preparation.key else "league_standby_no_wagers")
                    self.stop.wait(.5)
                    continue
                try:
                    signal = self.signals.get(timeout=.05)
                except queue.Empty:
                    with redirect_stdout(QuietOutput()):
                        self.maintain_staging(self.driver, core, browser_preparation, market_readiness, time.monotonic())
                        browser_preparation.step(self.driver, self.preparation,
                            self.lease.clock() < self.lease.until and self.last_feed is True,
                            time.time()+self.lease.server_offset, hold=self.hold_preparation())
                    continue
                current = signal
                signal.setdefault('_timing', {}).setdefault('dequeued', time.monotonic())
                self.pending_contracts.discard(signal["contract"])
                submission_may_have_run = False
                self.history_outcome(signal, "processing", "waiting")
                if not (signal.get('_preflight_authorized') and self.permits(signal) and
                        timestamp(signal['payload']['plan']['deadline']) > time.time()+self.lease.server_offset+.6) and not self.authorize(signal):
                    if self.last_authorize_reason == 'authorization_unavailable' and self.defer_readiness(signal, {'status': 'authorization_timeout'}):
                        continue
                    self.history_outcome(signal, "handled" if self.last_authorize_reason == "claimed_elsewhere" else "missed", self.last_authorize_reason)
                    continue
                signal['_preflight_authorized'] = True
                payload = signal["payload"]
                # Protocol 2 only: the plan is the sole decision. The client
                # executes it by comparison and never derives a stake or a floor.
                plan = payload["plan"]
                event, market = payload["event"], payload["market"]
                browser_preparation.publish("validating_signal", f"Validating model signal {signal['id']} for {event['player1']} vs {event['player2']}.", event["eventId"])
                try:
                    with redirect_stdout(QuietOutput()):
                        remaining = lambda: min(self.lease.until-self.lease.clock(),
                                                timestamp(plan['deadline'])-time.time()-self.lease.server_offset,
                                                timestamp(signal["expires_at"])-time.time()-self.lease.server_offset)
                        if remaining() <= .6:
                            self.history_outcome(signal, "missed", "signal_expired")
                            continue
                        turn_until = time.monotonic()+.3
                        readiness = market_readiness.prepare(self.driver, event, market,
                            valid=lambda: self.permits(signal), remaining=remaining, plan=plan)
                        if readiness.get("status") != "revealed":
                            if self.defer_readiness(signal, readiness):
                                continue
                            reason = "market_unavailable" if self.permits(signal) else "signal_expired"
                            self.history_outcome(signal, "handled" if reason == "claimed_elsewhere" else "missed", reason,
                                                 result={"readiness": readiness})
                            continue
                        self.history_outcome(signal, "processing", "waiting", result={"readiness": readiness})
                        # Protocol 2 claims only with the observed quote, inside the plan flow.
                        if self.device_test and not self.device_test.claim(signal):
                            self.lease.running = False
                            continue
                        if self.device_test:
                            self.device_test.update(phase="executing_one_opportunity")
                        outcome = next((row for row in market.get("outcomes") or []
                                        if row.get("selectionId") == plan["selectionId"]), None)
                        if outcome is None:
                            self.history_outcome(signal, "missed", "execution_error")
                            continue
                        if not readiness.get('staged'):
                            selected = self.staging.add(self.driver, core, event, market, outcome,
                                lambda: self.permits(signal), max(.001, turn_until-time.monotonic()))
                            if selected.get('status') not in ('clicked', 'selected'):
                                if self.defer_readiness(signal, selected):
                                    continue
                                self.history_outcome(signal, 'missed', 'market_unavailable')
                                continue
                        result = core.place_plan(self.driver, event, market, plan, outcome,
                                                 authorize_values=self.claim_values(signal, ledger),
                                                 validate=lambda: self.permits(signal),
                                                 deadline_budget=max(0, remaining()))
                    state = (result or {}).get("status", "not_submitted")
                    if ((result or {}).get('retryable') is True and (result or {}).get('submissionState') == 'not-submitted'
                            and not submission_may_have_run and self.defer_readiness(signal, result)):
                        continue
                    ledger.finish(self.account, signal["contract"], state)
                    signal.setdefault('_timing', {})['finished'] = time.monotonic()
                    if (result or {}).get("denied"):
                        # The denial reason is authoritative; the result payload
                        # would otherwise be re-interpreted as a non-submission.
                        self.history_outcome(signal, "handled" if result["denied"] == "claimed_elsewhere" else "missed",
                                             result["denied"])
                        emit("execution", status=state, message=f"Signal {signal['id']}: {state}")
                        continue
                    if state == "placed":
                        self.history_outcome(signal, "placed", "placed", result=result)
                    elif state == "unconfirmed" or (result or {}).get('submissionState') == 'uncertain' or (submission_may_have_run and (result or {}).get("submitted") is not False):
                        self.history_outcome(signal, "uncertain", "submission_uncertain", result=result)
                        self.lease.running = False
                        self.lease.revoke()
                    else:
                        self.history_outcome(signal, "missed", result_reason(result), result=result)
                    if self.device_test:
                        self.device_test.execution(result)
                        self.lease.running = False
                        self.lease.revoke()
                    emit("execution", status=state, message=f"Signal {signal['id']}: {state}")
                except Exception as error:
                    if submission_may_have_run or getattr(self.driver, '_tt_plan_uncertain', False) is True:
                        self.lease.running = False
                        self.lease.revoke()
                    self.history_outcome(signal, "uncertain" if submission_may_have_run else "missed",
                        "submission_uncertain" if submission_may_have_run else "browser_not_ready" if browser_connection_lost(error) else "execution_error")
                    if self.device_test and not self.readiness_only:
                        self.device_test.finish("execution_error")
                        self.lease.running = False
                    emit("execution", status="unconfirmed" if submission_may_have_run else "interrupted",
                         message="Execution interrupted. Check DraftKings history for any uncertain submission; this contract will not be retried. Fresh opportunities can resume after recovery.")
                    raise
        finally:
            self.provider_ready.clear()
            if self.driver:
                if original_execute:
                    self.driver.execute_script = original_execute
                self.driver = None
            with redirect_stdout(QuietOutput()):
                owner.detach()
            ledger.connection.close()

    def start(self, max_bankroll_risk_percent=DEFAULT_RISK_PERCENT, resume=False, generation=None,
              conservative_level=DEFAULT_LEVEL, kelly_multiplier=DEFAULT_KELLY):
        validate_risk(max_bankroll_risk_percent, kelly_multiplier)
        client_settings(conservative_level)
        if type(resume) is not bool:
            raise ValueError("Invalid session resume intent")
        generation = self.run_generation if generation is None else generation
        with self.run_lock:
            if generation != self.run_generation or self.stop.is_set():
                return
            if self.lease.running:
                return
            if self.execution_thread and self.execution_thread.is_alive():
                raise RuntimeError("wait_for_previous_session_to_stop")
            status = self.request("/api/v1/status/", authenticated=True)
            if not status["enabled"] or not status["client_supported"]:
                raise RuntimeError("subscription_or_update_required")
            # Network work is outside this lock. The final generation check and
            # enabling handoff are atomic with Stop, so a delayed Start cannot
            # clear a Stop that arrived between its response and thread launch.
            with self.intent_lock:
                if generation != self.run_generation or self.stop.is_set():
                    return
                self.account, self.stake = status["account_id"], 0
                self.max_bankroll_risk_percent = max_bankroll_risk_percent
                self.conservative_level, self.kelly_multiplier = conservative_level, kelly_multiplier
                self.provider_ready.clear()
                self.browser_alerted = False
                self.browser_recovering = False
                self.resume_run = resume
                if not resume:
                    self.location_recheck = LocationRecheck()
                self.preserve_browser_on_exit = False
                self.run_cancel.clear()
                if self.device_test:
                    self.device_test.begin(0)
                    self.device_test.update(max_bankroll_risk_percent=max_bankroll_risk_percent,
                                            kelly_multiplier=kelly_multiplier, conservative_level=conservative_level)
                    emit("status", message="Device test: scan and candidate navigation only; wager execution is disabled." if self.scan_only else "Device test: login and Table Tennis readiness only; wager execution is disabled." if self.readiness_only else "Device test: one execution opportunity using the selected conservative policy and fractional Kelly.")
                self.lease.running = True
                emit("session", running=True)
                self.execution_thread = threading.Thread(target=lambda: self.guarded(self.execute, execution=True), daemon=True)
                self.execution_thread.start()

    def guarded(self, action, *, execution=False):
        try:
            action()
        except Exception as error:
            text = str(error)
            known = {
                "The project Python environment was not found": "packaged_launcher_requires_venv",
                "Chrome startup failed": "chrome_start_failed",
                "Could not start dedicated Chrome": "chrome_start_failed",
                "Chrome was not found": "chrome_not_installed",
                "Restart the application for another device test": "device_test_already_finished",
                "subscription_or_update_required": "subscription_or_update_required",
                "invalid_grant": "sign_in_again",
                "sign_in_required": "sign_in_again",
            }
            error_code = next((code for message, code in known.items() if message in text), "operation_failed")
            if self.device_test and execution:
                self.device_test.update(error_type=type(error).__name__, error_code=error_code)
            if execution:
                self.lease.running = False
                self.provider_ready.clear()
                self.lease.revoke()
            message = "Sign in to Ember through the browser before starting or checking for updates." if error_code == "sign_in_again" else f"Operation could not complete ({type(error).__name__}, {error_code}). Check connection and account status."
            emit("error", message=message)
            if not self.lease.running and (not self.execution_thread or not self.execution_thread.is_alive()):
                self.stopped()

    def update_token(self):
        self.ensure_access()
        if self.access_token:
            emit("credential", token=self.access_token)

    def main(self):
        threading.Thread(target=self.stream, daemon=True).start()
        threading.Thread(target=self.history_loop, daemon=True).start()
        emit("ready", version=VERSION)
        if self.device_test:
            emit("status", message="Scan-only real-device validation is enabled; wager execution is disabled." if self.scan_only else "Login-only real-device validation is enabled; wager execution is disabled." if self.readiness_only else "One-opportunity real-device validation is enabled for this launch.")
        quit_requested = False
        for raw in sys.stdin:
            try:
                command = json.loads(raw)
                action = command.get("command")
                if action == "login":
                    threading.Thread(target=lambda api=command["api"]: self.guarded(lambda: self.login(api)), daemon=True).start()
                elif action == "start":
                    generation = self.run_generation
                    threading.Thread(target=lambda options=dict(command), generation=generation:
                        self.guarded(lambda: self.start(options.get("max_bankroll_risk_percent", DEFAULT_RISK_PERCENT),
                            options.get("resume", False), generation, options.get("conservative_level", DEFAULT_LEVEL),
                            options.get("kelly_multiplier", DEFAULT_KELLY))), daemon=True).start()
                elif action == "stop":
                    self.request_stop()
                elif action == "missed_history":
                    history_filter, offset = command.get("filter", self.history_filter), command.get("offset", 0)
                    if history_filter not in {"missed", "uncertain", "all"} or type(offset) is not int or not 0 <= offset <= 1_000_000:
                        raise ValueError("Invalid history page")
                    self.history_filter, self.history_offset = history_filter, offset
                    self.publish_history()
                elif action == "sync_missed_history":
                    self.history_force_sync.set()
                    self.history_wake.set()
                elif action == "update_token":
                    threading.Thread(target=lambda: self.guarded(self.update_token), daemon=True).start()
                elif action == "quit":
                    quit_requested = True
                    break
            except (ValueError, KeyError):
                emit("error", message="Invalid application command")
        if not quit_requested:
            # A broken host pipe is not a human browser X/Stop/quit. Pause
            # execution and preserve Chrome so a replacement host/worker can
            # reattach; never let generic EOF cleanup close the browser.
            self.preserve_browser_on_exit = True
            LifecycleLog(self.data_dir/"browser-lifecycle.jsonl").record("host_connection_lost")
        self.request_stop()
        self.stop.set()
        if self.execution_thread:
            self.execution_thread.join(timeout=8)


if __name__ == "__main__":
    Client().main()
