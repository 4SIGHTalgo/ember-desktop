"""Fault injection against owned local fixtures only. No provider/wager requests."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import io
from pathlib import Path
import queue
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import websocket
import psutil
from selenium.common.exceptions import InvalidSessionIdException, TimeoutException, WebDriverException

from ember_browser_lifecycle import (BrowserCancelled, BrowserClosed, BrowserWaiting, CommandLog, LifecycleLog, OwnedBrowser,
                                     ProcessExitWitness, ProfileLease)
from ember_client import Client, Ledger
from ember_navigation import NavigationPending
import hashlib


def test_directory():
    # Native Chrome extension paths can exceed Windows' legacy path limit when
    # this suite runs from a nested release worktree.
    parent = Path(tempfile.gettempdir())/"opencode" if os.name == "nt" else Path(__file__).parent/"desktop/build"
    parent.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix="browser-test-", dir=parent)


def cleanup_directory(directory):
    deadline = time.monotonic()+15
    while True:
        try:
            directory.cleanup()
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.1)


class BrowserLifecycleTests(unittest.TestCase):
    def setUp(self):
        directory = test_directory()
        self.addCleanup(cleanup_directory, directory)
        self.directory = Path(directory.name)
        self.browser = OwnedBrowser(self.directory, ("https://example.com", "1"))
        self.addCleanup(self.browser.release)

    def test_profile_is_account_and_api_scoped_and_cannot_be_owned_twice(self):
        second = OwnedBrowser(self.directory, ("https://example.com", "2"))
        third = OwnedBrowser(self.directory, ("https://different.example", "1"))
        self.addCleanup(second.release)
        self.addCleanup(third.release)
        self.assertNotEqual(self.browser.profile, second.profile)
        self.assertNotEqual(self.browser.profile, third.profile)
        self.browser.lease.acquire()
        other = ProfileLease(self.browser.folder/"owner.lock")
        with self.assertRaises(BrowserWaiting):
            other.acquire()
        self.browser.lease.release()
        other.acquire()
        other.release()

    def test_stop_before_start_cannot_launch_or_attach(self):
        with patch("ember_browser_lifecycle.subprocess.Popen") as launch, patch("ember_browser_lifecycle.create_owned_chrome") as attach:
            with self.assertRaises(BrowserCancelled):
                self.browser.open(lambda: False)
        launch.assert_not_called()
        attach.assert_not_called()

    def test_resumed_worker_with_no_owned_browser_cannot_override_a_possible_human_close(self):
        self.browser.resume = True
        with patch.object(self.browser, "_owners", return_value=[]), patch.object(self.browser, "_spawn") as launch:
            with self.assertRaises(BrowserClosed):
                self.browser.open(lambda: True)
        launch.assert_not_called()

    def test_a_previous_sessions_browser_still_exiting_is_replaced_on_an_explicit_start(self):
        # Live 2026-10-28 11:44: a Start 10 s after Stop adopted the exiting browser and stopped as a close.
        exiting = Mock(pid=4242)
        exiting.create_time.return_value = 1.0
        exiting.cmdline.return_value = []
        class Spawned(Exception):
            pass
        cases = ((self.browser, False, Spawned), (OwnedBrowser(self.directory, ("https://example.com", "resumed"), resume=True), False, BrowserClosed),
                 (OwnedBrowser(self.directory, ("https://example.com", "seen")), True, BrowserClosed))
        for browser, seen, expected in cases:
            with self.subTest(resume=browser.resume, seen=seen):
                self.addCleanup(browser.release)
                browser.ever_open = seen  # A browser this owner saw open, then closed, is a human close.
                with patch.object(browser, "_owners", return_value=[exiting]), patch("ember_browser_lifecycle.ProcessExitWitness"), \
                        patch.object(browser, "inspect", return_value={"state": "closed", "pages": 0}), \
                        patch.object(browser, "_spawn", side_effect=Spawned) as launch:
                    with self.assertRaises(expected):
                        browser.open(lambda: True)
                self.assertEqual(launch.call_count, 1 if expected is Spawned else 0)
                self.assertEqual(browser.closed, expected is BrowserClosed)

    def test_clean_and_unknown_exits_stop_while_abnormal_exit_permits_browser_restart(self):
        for state, code, expected in (("closed", 0, "closed"), ("unknown", None, "closed"), ("crashed", 0xC0000005, "browser_restart")):
            self.browser.closed = False
            with patch.object(self.browser, "inspect", return_value={"state": state, "exit_code": code}):
                self.assertEqual(self.browser.recovery(InvalidSessionIdException("lost")), expected)

    def test_live_browser_with_a_failed_driver_is_never_quit_or_relaunched(self):
        driver = self.browser.driver = Mock()
        with patch.object(self.browser, "inspect", return_value={"state": "open", "pages": 1}), \
                patch("ember_browser_lifecycle.close_owned_driver") as detach, patch.object(self.browser, "_spawn") as launch:
            self.assertEqual(self.browser.recovery(InvalidSessionIdException("lost")), "driver_reconnect")
        detach.assert_called_once_with(driver)
        driver.quit.assert_not_called()
        driver.execute_cdp_cmd.assert_not_called()
        launch.assert_not_called()

    def test_unresponsive_but_live_browser_is_waited_for_not_killed(self):
        self.browser.process = Mock()
        with patch.object(self.browser, "inspect", return_value={"state": "waiting"}):
            self.assertEqual(self.browser.recovery(WebDriverException("unreachable")), "browser_wait")
        self.browser.process.terminate.assert_not_called()
        self.browser.process.kill.assert_not_called()

    def test_diagnostics_never_contain_raw_browser_errors_or_secrets(self):
        path = self.directory/"diagnostics.jsonl"
        LifecycleLog(path).record("browser_fault", error=RuntimeError("cookie=private; password=private; body=private"))
        content = path.read_text()
        self.assertNotIn("private", content)
        self.assertEqual(json.loads(content)["error_type"], "RuntimeError")

    def test_diagnostics_record_only_the_startup_stage_code(self):
        path = self.directory/"stage-diagnostics.jsonl"
        LifecycleLog(path).record("browser_fault",
                                  error=BrowserWaiting("browser_endpoint_unavailable; password=private"),
                                  stage="browser_endpoint_unavailable")
        content = path.read_text()
        self.assertNotIn("private", content)
        self.assertEqual(json.loads(content)["stage"], "browser_endpoint_unavailable")

    def test_fault_records_only_a_short_ascii_activity_label(self):
        path = self.directory/"diagnostics.jsonl"
        LifecycleLog(path).record("browser_fault", error=RuntimeError("boom"), activity="script:REVEAL_SCRIPT")
        self.assertEqual(json.loads(path.read_text())["activity"], "script:REVEAL_SCRIPT")
        LifecycleLog(path).record("browser_fault", activity="  ")
        LifecycleLog(path).record("browser_fault", activity="préparer:100")
        rows = [json.loads(line) for line in path.read_text().splitlines()][1:]
        self.assertNotIn("activity", rows[0])
        self.assertEqual(rows[1]["activity"], "prparer:100")

    def test_attach_phases_record_finite_numbers_only(self):
        path = self.directory/"diagnostics.jsonl"
        LifecycleLog(path).record("driver_attached", phases={"browser": 12.6, "driver": float("nan"), "select": "secret-text",
                                                             "x"*40: 1})
        self.assertEqual(json.loads(path.read_text())["phases_ms"], {"browser": 13, "x"*24: 1})

    def test_recovery_journals_the_last_activity_with_the_fault(self):
        self.browser.process = Mock()
        with patch.object(self.browser, "inspect", return_value={"state": "waiting"}):
            self.assertEqual(self.browser.recovery(WebDriverException("unreachable"), activity="prepare:100"),
                             "browser_wait")
        row = json.loads((self.directory/"browser-lifecycle.jsonl").read_text().splitlines()[-1])
        self.assertEqual(row["event"], "browser_fault")
        self.assertEqual(row["activity"], "prepare:100")

    def test_a_fault_records_which_of_our_calls_raised_but_never_its_message(self):
        def provider_page():
            raise TimeoutException("timeout: secret-fixture-text")
        try:
            provider_page()
        except TimeoutException as error:
            LifecycleLog(self.directory/"faults.jsonl").record("browser_fault", error=error)
        text = (self.directory/"faults.jsonl").read_text()
        self.assertNotIn("secret-fixture-text", text)
        where = json.loads(text)["where"]
        self.assertEqual(where[-1].split(":")[0::2], ["test_ember_browser_lifecycle", "provider_page"])
        LifecycleLog(self.directory/"plain.jsonl").record("browser_fault", error=WebDriverException("never raised"))
        self.assertNotIn("where", json.loads((self.directory/"plain.jsonl").read_text()))

    def test_command_log_names_slow_commands_and_totals_a_minute_without_arguments(self):
        clock = [0.0]
        path = self.directory/"driver-commands.jsonl"
        log = CommandLog(path, clock=lambda: clock[0])
        named, secret = "return 1", "return 'secret-fixture-value'"
        log.names, log.activity = {id(named): "READ_SLIP_SCRIPT"}, lambda: "prepare:100"
        log("w3cExecuteScript", {"script": named, "args": ["secret-arg"]}, .1)  # Fast: totalled, not listed.
        log("w3cExecuteScript", {"script": named, "args": []}, .3)
        log("w3cExecuteScript", {"script": secret, "args": []}, .5)
        log("w3cExecuteScript", {"script": "/* isDisplayed */return (function(){})", "args": []}, .25)
        clock[0] = 61
        log("getCurrentUrl", {}, .01)
        content = path.read_text()
        for private in ("secret-fixture-value", "secret-arg"):
            self.assertNotIn(private, content)
        rows = [json.loads(line) for line in content.splitlines()]
        self.assertEqual([(r["label"], r["ms"], r["activity"]) for r in rows if r["event"] == "slow_command"],
                         [("READ_SLIP_SCRIPT", 300.0, "prepare:100"),
                          ("inline:" + hashlib.sha256(secret.encode()).hexdigest()[:10], 500.0, "prepare:100"),
                          ("isDisplayed", 250.0, "prepare:100")])
        minute = rows[-1]
        self.assertEqual((minute["event"], minute["commands"], minute["busy_ms"]), ("command_minute", 5, 1160))
        self.assertEqual(minute["top"][0], ["inline:" + hashlib.sha256(secret.encode()).hexdigest()[:10], 1, 500, 500])
        self.assertEqual(minute["top"][1], ["READ_SLIP_SCRIPT", 2, 400, 300])
        log("getCurrentUrl", {}, .01)  # A new window starts after each minute row.
        self.assertEqual(len(path.read_text().splitlines()), len(rows))

    def test_stale_driver_cleanup_requires_exact_process_creation_and_driver_port(self):
        process = Mock()
        process.create_time.return_value = 123
        process.name.return_value = "chromedriver.exe"
        process.cmdline.return_value = ["chromedriver.exe", "--port=54321"]
        with patch("ember_browser_lifecycle.psutil.Process", return_value=process):
            for created, port in ((456, 54321), (123, 12345)):
                self.browser.stale_driver = {"pid": 100, "created": created, "port": port}
                self.browser._dispose_stale_driver()
                process.kill.assert_not_called()
            self.browser.stale_driver = {"pid": 100, "created": 123, "port": 54321}
            self.browser._dispose_stale_driver()
        process.kill.assert_called_once()

    def test_preserving_service_never_sends_a_graceful_browser_shutdown(self):
        from driver_lifecycle import OwnedChromeService
        service = OwnedChromeService(preserve_external_browser=True)
        process = service.process = Mock()
        process.poll.return_value = None
        with patch.object(service, "send_remote_shutdown_command") as shutdown, patch.object(service, "_terminate_process") as graceful:
            service.stop()
        process.kill.assert_called_once()
        process.terminate.assert_not_called()
        shutdown.assert_not_called()
        graceful.assert_not_called()
        service.process = None

    @unittest.skipUnless(os.name == "nt" or sys.platform == "darwin", "Windows/macOS exit evidence")
    def test_exit_witness_preserves_clean_and_abnormal_status_after_process_reaping(self):
        for code in (0, 7):
            with self.subTest(code=code):
                process = subprocess.Popen([sys.executable, "-c", f"import sys;sys.stdin.buffer.read(1);sys.exit({code})"],
                                           stdin=subprocess.PIPE)
                witness = ProcessExitWitness(process.pid)
                try:
                    self.assertEqual(witness.poll(), (False, None))
                    process.stdin.write(b"x")
                    process.stdin.close()
                    self.assertEqual(process.wait(timeout=10), code)
                    self.assertEqual(witness.poll(), (True, code))
                    self.assertEqual(witness.poll(), (True, code))
                finally:
                    witness.close()
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=10)
                    if not process.stdin.closed:
                        process.stdin.close()

    def test_exact_ownership_marker_and_profile_are_required_before_adoption(self):
        self.browser.nonce = "a"*48
        process = Mock(info={"name": "chrome.exe", "cmdline": ["chrome.exe", f"--user-data-dir={self.browser.profile}",
            "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1", "--ember-owned-browser="+"a"*48]})
        with patch("ember_browser_lifecycle.psutil.process_iter", return_value=[process]):
            self.assertEqual(self.browser._owners(), [process])
            process.info["cmdline"][-1] = "--ember-owned-browser="+"b"*48
            with self.assertRaises(BrowserWaiting):
                self.browser._owners()
            process.info["cmdline"][-1] = "--ember-owned-browser="+"a"*48
            process.info["cmdline"][-2] = "--remote-debugging-address=0.0.0.0"
            with self.assertRaises(BrowserWaiting):
                self.browser._owners()

    def test_explicit_ports_can_only_adopt_the_same_owned_profile_and_marker(self):
        self.browser.nonce, self.browser.port = "a"*48, 54321
        process = Mock(info={"name": "chrome.exe", "cmdline": ["chrome.exe", f"--user-data-dir={self.browser.profile}",
            "--remote-debugging-port=54321", "--remote-debugging-address=127.0.0.1", "--ember-owned-browser="+"a"*48]})
        with patch("ember_browser_lifecycle.psutil.process_iter", return_value=[process]):
            self.assertEqual(self.browser._owners(), [process])
            process.info["cmdline"][2] = "--remote-debugging-port=54322"
            with self.assertRaises(BrowserWaiting):
                self.browser._owners()

    def test_selected_port_requires_the_owned_process_and_loopback_listener(self):
        self.browser.process, self.browser.port = Mock(pid=100), 54321
        def listener(pid=100, host="127.0.0.1"):
            return SimpleNamespace(status=psutil.CONN_LISTEN, pid=pid, laddr=SimpleNamespace(ip=host, port=54321))
        with patch("ember_browser_lifecycle.psutil.net_connections") as connections:
            for values, expected in (([], False), ([listener()], True), ([listener(pid=200)], False),
                    ([listener(host="0.0.0.0")], False), ([listener(), listener(pid=200)], False)):
                connections.return_value = values
                self.assertEqual(self.browser._owns_listener(), expected)


class ClientRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = test_directory()
        self.addCleanup(cleanup_directory, directory)
        with patch.dict(os.environ, {"LOCALAPPDATA": directory.name, "EMBER_DEVICE_TEST_ONCE": "", "EMBER_DEVICE_TEST_LOGIN_ONLY": "",
                "EMBER_DEVICE_TEST_SCAN_ONLY": "", "EMBER_DEVICE_BROWSER_TICKET": ""}), patch.dict(sys.modules, {"keyring": Mock()}):
            self.client = Client()
        self.client.api, self.client.account = "https://example.com", "1"
        self.client.lease.running = True
        now = datetime.now(timezone.utc)
        self.client.lease.update({"enabled": True, "client_supported": True, "server_time": now.isoformat(),
            "access_until": (now+timedelta(hours=1)).isoformat(), "lease_seconds": 5})

    def test_update_or_account_control_error_cannot_stop_a_running_session(self):
        self.client.provider_ready.set()
        before = self.client.lease.until
        def fail():
            raise RuntimeError("invalid_grant")
        with patch("ember_client.emit") as emit:
            self.client.guarded(fail)
        self.assertTrue(self.client.lease.running)
        self.assertTrue(self.client.provider_ready.is_set())
        self.assertEqual(self.client.lease.until, before)
        self.assertFalse(any(call.args == ("stopped",) for call in emit.call_args_list))

    def test_host_pipe_loss_preserves_browser_but_explicit_quit_retains_stop_intent(self):
        for command, recoverable in (("", True), ('{"command":"quit"}\n', False)):
            with self.subTest(recoverable=recoverable):
                client = self.client
                client.lease.running = True
                client.stop.clear()
                client.run_cancel.clear()
                client.preserve_browser_on_exit = False
                client.execution_thread = Mock()
                client.execution_thread.is_alive.return_value = True
                with patch("ember_client.sys.stdin", io.StringIO(command)), patch("ember_client.threading.Thread"), patch("ember_client.emit") as emit:
                    client.main()
                    client.stopped()
                self.assertEqual(client.preserve_browser_on_exit, recoverable)
                self.assertFalse(client.lease.running)
                self.assertTrue(client.stop.is_set())
                stopped = [call.kwargs for call in emit.call_args_list if call.args == ("stopped",)]
                self.assertEqual(stopped[-1]["recoverable"], recoverable)

    def test_control_pipe_fault_cannot_turn_stop_cleanup_into_a_browser_close(self):
        owner = Mock(retained=False)
        def disconnected(*_args):
            self.client.preserve_browser_on_exit = True
            self.client.request_stop()
        with patch("ember_client.OwnedBrowser", return_value=owner), patch("ember_client.ActiveSessionPower"), \
                patch("ember_client.emit"), patch.object(self.client, "execute_browser", side_effect=disconnected):
            self.client.execute()
        owner.release.assert_called_once_with(close=False)

    def test_stop_during_status_request_cancels_start_instead_of_reopening_later(self):
        self.client.lease.running = False
        def status(*_args, **_kwargs):
            self.client.request_stop()
            return {"enabled": True, "client_supported": True, "account_id": "1"}
        self.client.request = status
        with patch("ember_client.emit"), patch("ember_client.threading.Thread") as thread:
            self.client.start(5)
        thread.assert_not_called()
        self.assertFalse(self.client.lease.running)
        self.assertTrue(self.client.run_cancel.is_set())

    def test_stop_at_the_final_start_handoff_cannot_be_overwritten(self):
        client = self.client
        client.lease.running = False
        client.request = Mock(return_value={"enabled": True, "client_supported": True, "account_id": "1"})
        checked, release, stopping = threading.Event(), threading.Event(), threading.Event()
        original_clear = client.provider_ready.clear
        def delay_handoff():
            if threading.current_thread().name == "start-handoff-test":
                checked.set()
                release.wait(3)
            original_clear()
        def stop():
            stopping.set()
            client.request_stop()
        with patch.object(client.provider_ready, "clear", side_effect=delay_handoff), \
                patch.object(client, "execute"), patch("ember_client.emit"):
            starter = threading.Thread(target=lambda: client.start(5), name="start-handoff-test")
            stopper = threading.Thread(target=stop)
            starter.start()
            self.assertTrue(checked.wait(3))
            stopper.start()
            self.assertTrue(stopping.wait(3))
            time.sleep(.05)  # Let Stop reach the serialized handoff while Start is suspended.
            release.set()
            starter.join(5)
            stopper.join(5)
            if client.execution_thread:
                client.execution_thread.join(5)
        self.assertFalse(starter.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertFalse(client.lease.running)
        self.assertTrue(client.run_cancel.is_set())

    def test_recovery_keeps_pending_quotes_fail_closed_and_never_downgrades_a_submitted_contract(self):
        from test_ember_history import history_signal
        signal = history_signal()
        self.client.provider_ready.set()
        self.client.receive_opportunity(signal, self.client.history_scope())
        self.client.signals.put(signal)
        ledger = Ledger(self.client.data_dir/"execution.sqlite3")
        ledger.reserve(self.client.account, signal["contract"])
        self.client.history_outcome(signal, "submitting", "waiting")
        self.client.pause_browser()
        self.assertFalse(self.client.provider_ready.is_set())
        self.assertEqual(self.client.lease.until, 0)
        # A transient pause keeps the pending quote for a fast reconnect; the
        # revoked lease and cleared provider permission keep it fail-closed.
        self.assertEqual(self.client.signals.get_nowait(), signal)
        self.assertFalse(self.client.permits(signal))
        self.assertFalse(ledger.reserve(self.client.account, signal["contract"]))
        # The pause itself never resolves a possibly-submitted contract; only
        # post-recovery processing (or login reconciliation) may, as uncertain.
        row = self.client.history.snapshot(self.client.history_scope())["records"][0]
        self.assertEqual(row["outcome"], "submitting")
        self.client.history_outcome(signal, "missed", "browser_not_ready")
        row = self.client.history.snapshot(self.client.history_scope())["records"][0]
        self.assertEqual(row["outcome"], "uncertain")
        ledger.connection.close()

    def test_fault_recovers_without_stopped_event_and_stop_in_backoff_prevents_any_reopen(self):
        owner = Mock(retained=False)
        owner.recovery.return_value = "driver_reconnect"
        calls = []
        def execute(_owner, active):
            calls.append(active())
            if len(calls) == 1:
                raise InvalidSessionIdException("driver lost")
            self.client.request_stop()
        with patch("ember_client.OwnedBrowser", return_value=owner), patch("ember_client.ActiveSessionPower"), \
                patch("ember_client.emit") as emit, patch.object(self.client, "execute_browser", side_effect=execute), \
                patch.object(self.client.run_cancel, "wait", return_value=False):
            self.client.execute()
        self.assertEqual(calls, [True, True])
        states = [call.args[0] for call in emit.call_args_list]
        self.assertLess(states.index("browser_recovery"), states.index("stopped"))
        self.assertFalse(self.client.browser_alerted)
        owner.release.assert_called_once_with(close=True)

        self.client.lease.running = True
        self.client.run_cancel.clear()
        owner.reset_mock()
        with patch("ember_client.OwnedBrowser", return_value=owner), patch("ember_client.ActiveSessionPower"), \
                patch("ember_client.emit"), patch.object(self.client, "execute_browser", side_effect=InvalidSessionIdException("lost")) as execute, \
                patch.object(self.client.run_cancel, "wait", side_effect=lambda _: self.client.request_stop()):
            self.client.execute()
        self.assertEqual(execute.call_count, 1)
        owner.release.assert_called_once_with(close=True)

    def test_startup_wait_failure_reports_the_exact_stage_and_stays_fail_closed(self):
        owner = Mock(retained=False)
        owner.recovery.return_value = "browser_wait"
        with patch("ember_client.OwnedBrowser", return_value=owner), patch("ember_client.ActiveSessionPower"), \
                patch("ember_client.emit") as emit, patch.object(self.client, "execute_browser",
                side_effect=BrowserWaiting("browser_endpoint_unavailable")), \
                patch.object(self.client.run_cancel, "wait", side_effect=lambda *_: self.client.request_stop()):
            self.client.execute()
        recoveries = [call.kwargs for call in emit.call_args_list if call.args[:1] == ("browser_recovery",)]
        self.assertEqual(len(recoveries), 1)
        self.assertEqual(recoveries[0]["stage"], "browser_endpoint_unavailable")
        self.assertIn("debugging endpoint", recoveries[0]["message"])
        self.assertIn("nothing was submitted", recoveries[0]["message"])
        owner.log.record.assert_any_call("recovery_wait", attempt=1, stage="browser_endpoint_unavailable")
        # Fail closed: the startup wait failure revoked permission and queued nothing.
        self.assertFalse(self.client.lease.running)
        self.assertFalse(self.client.provider_ready.is_set())
        self.assertTrue(self.client.signals.empty())
        owner.release.assert_called_once_with(close=True)

    def test_unknown_or_absent_startup_stage_keeps_the_generic_recovery_message(self):
        for error in (BrowserWaiting("brand_new_wait_stage"), WebDriverException("chrome not reachable; password=private")):
            with self.subTest(error=type(error).__name__):
                owner = Mock(retained=False)
                owner.recovery.return_value = "browser_wait"
                self.client.lease.running = True
                self.client.run_cancel.clear()
                self.client.stop.clear()
                with patch("ember_client.OwnedBrowser", return_value=owner), patch("ember_client.ActiveSessionPower"), \
                        patch("ember_client.emit") as emit, patch.object(self.client, "execute_browser",
                        side_effect=error), \
                        patch.object(self.client.run_cancel, "wait", side_effect=lambda *_: self.client.request_stop()):
                    self.client.execute()
                recoveries = [call.kwargs for call in emit.call_args_list if call.args[:1] == ("browser_recovery",)]
                self.assertEqual(len(recoveries), 1)
                self.assertIn("DraftKings connection interrupted", recoveries[0]["message"])
                self.assertNotIn("password=private", json.dumps(recoveries[0]))
                self.assertNotIn("brand_new_wait_stage", recoveries[0]["message"])
                expected = "brand_new_wait_stage" if isinstance(error, BrowserWaiting) else None
                self.assertEqual(recoveries[0]["stage"], expected)

    def test_actual_execution_recovery_never_replays_an_uncertain_contract(self):
        import tt_market_exec as core
        from test_ember_client import ProtocolV2Tests
        client = self.client
        fixture = ProtocolV2Tests.__new__(ProtocolV2Tests)
        original = fixture.plan_signal()
        client.receive_opportunity(original, client.history_scope())
        client.signals.put(original)
        drivers = [Mock(current_url=core.LEAGUE_URL), Mock(current_url=core.LEAGUE_URL)]
        for driver in drivers:
            driver.execute_script.return_value={'present':False,'status':'empty','stagingSafe':True,'legs':[]}
        def dispatch(script,*args):
            if script.endswith(core.CONFIRM_PLAN_SCRIPT):
                raise InvalidSessionIdException('lost during submit')
            return {'present':False,'status':'empty','stagingSafe':True,'legs':[]}
        drivers[0].execute_script.side_effect = dispatch
        owner = Mock(retained=False)
        owner.open.side_effect = drivers
        owner.recovery.return_value = "driver_reconnect"
        gate = Mock()
        seen, checked = [], []

        def poll(driver, _reader):
            if driver is drivers[1] and not checked:
                before = (client.provider_ready.is_set(), client.lease.until, client.signals.empty())
                duplicate = fixture.plan_signal()
                duplicate["id"] = 2
                client.receive_opportunity(duplicate, client.history_scope())
                checked.append((before, duplicate["_history_handled"], client.signals.empty()))
                now = datetime.now(timezone.utc)
                client.lease.update({"enabled": True, "client_supported": True, "server_time": now.isoformat(),
                    "access_until": (now+timedelta(hours=1)).isoformat(), "lease_seconds": 5})
                fresh = fixture.plan_signal()
                fresh["id"] = 3
                fresh["contract"] = "200:split"
                fresh["payload"]["event"]["eventId"] = "200"
                fresh["payload"]["market"]["eventId"] = "200"
                client.receive_opportunity(fresh, client.history_scope())
                client.signals.put(fresh)
            return {"ready": True, "signed_in": True, "signed_out_confirmed": False,
                    "provider_can_bet": True, "native_wagering_grant": True,
                    "location": {"state": "unknown", "blocked": False}, "message": None}

        def place(driver, event, *_args, **_kwargs):
            seen.append(event["eventId"])
            if driver is drivers[0]:
                ledger = Ledger(client.data_dir/"execution.sqlite3")
                self.assertTrue(ledger.reserve_signal(client.account, original))
                ledger.connection.close()
                driver.execute_script(core.CONFIRM_PLAN_SCRIPT)
            client.request_stop()
            return {"status": "skipped", "submitted": False,
                    "reason": "the final price is below the authorized price floor"}

        gate.poll.side_effect = poll
        with ExitStack() as stack:
            for setting in (patch("ember_client.OwnedBrowser", return_value=owner), patch("ember_client.ActiveSessionPower"),
                patch("ember_client.emit"), patch("ember_client.validation_ticket", return_value=None),
                patch("draftkings_login.allow_location_permissions"), patch("draftkings_login.goto_tt_elite"),
                patch("ember_provider_login.ProviderLoginGate", return_value=gate), patch("draftkings_session.SessionMaintenance"),
                patch.object(core, "prepare_browser_execution"), patch.object(core, "ROOT", client.data_dir),
                patch.object(core, "SLIP_CAPTURE", client.data_dir/"slip.json"),
                patch("ember_market_readiness.MarketReadiness.prepare", return_value={"status": "revealed", "staged": True}),
                patch("ember_market_readiness.MarketReadiness.open", return_value={"status": "opened"}),
                patch("ember_market_readiness.MarketReadiness.wait", return_value={"status": "revealed"}),
                patch.object(client, "authorize", side_effect=lambda signal, claim=False: client.permits(signal)),
                patch.object(client.run_cancel, "wait", return_value=False)):
                stack.enter_context(setting)
            stack.enter_context(patch.object(core, "place_plan", side_effect=place))
            client.execute()
        self.assertEqual(seen, ["100"])
        self.assertEqual(checked, [])
        owner.open.assert_called_once()
        owner.recovery.assert_not_called()
        self.assertFalse(client.lease.running)
        rows = {row["contract"]: row for row in client.history.snapshot(client.history_scope())["records"]}
        self.assertEqual(rows["100:split"]["outcome"], "uncertain")
        ledger = Ledger(client.data_dir/"execution.sqlite3")
        self.assertFalse(ledger.reserve(client.account, "100:split"))
        ledger.connection.close()

@unittest.skipUnless(os.name == "nt", "Native Windows startup compatibility")
class NativeBrowserStartupTests(unittest.TestCase):
    def test_native_startup_and_worker_reattachment_preserve_standard_browser_mode(self):
        directory = test_directory()
        self.addCleanup(cleanup_directory, directory)
        browser = OwnedBrowser(directory.name, ("https://example.com", "native-startup"), headless=False)
        self.addCleanup(lambda: browser.release(close=True))
        driver = browser.open(lambda: True)
        driver.get("data:text/html,<title>Ember local startup regression check</title>")
        self.assertFalse(browser.auto_port)
        self.assertTrue(browser._owns_listener())
        self.assertFalse(driver.execute_script("return navigator.webdriver === true"))
        original, pid = browser, browser.process.pid
        browser.release(close=False)
        browser = OwnedBrowser(directory.name, ("https://example.com", "native-startup"), resume=True, headless=False)
        driver = browser.open(lambda: True)
        self.assertEqual(browser.process.pid, pid)
        self.assertFalse(browser.auto_port)
        self.assertFalse(driver.execute_script("return navigator.webdriver === true"))
        browser.release(close=True)
        original.process.wait(timeout=20)


class LocalFixture(BaseHTTPRequestHandler):
    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass  # Expected when the owned browser is deliberately crashed.

    def do_GET(self):
        if self.path.startswith("/slow"):
            time.sleep(3)  # A document that commits well after the navigation gate's brief wait.
        content = b'<!doctype html><title>Owned recovery fixture</title><button onclick="window.wagerClicks++">Fixture only</button><script>window.wagerClicks=0;</script>'
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *_):
        pass


class OwnedBrowserFaultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), LocalFixture)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        directory = test_directory()
        self.addCleanup(cleanup_directory, directory)
        self.directory = Path(directory.name)
        self.browser = OwnedBrowser(self.directory, ("https://example.com", "fixture"), headless=True)
        self.addCleanup(lambda: self.browser.release(close=True))
        self.driver = self.browser.open(lambda: True)
        self.driver.get(self.url)
        self.wait(lambda: self.driver.execute_script("return document.readyState") == "complete")

    def wait(self, predicate):
        deadline = time.monotonic()+8
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except NavigationPending:
                pass  # The fixture page has not committed yet; the command was not sent.
            time.sleep(.1)
        self.fail("Owned fixture did not reach the expected state")

    def test_a_reattach_resumes_in_the_preferred_tab_instead_of_flipping_to_the_first_handle(self):
        # Live 2026-10-28: each recovery reselected handles[0], re-activating a background DK tab whose
        # 10-12 s wake-up tripped the next command timeout, so recovery looped every ~15 s.
        first = self.driver.current_window_handle
        self.driver.switch_to.new_window("tab")
        self.driver.get(self.url+"second")
        second = self.driver.current_window_handle
        for prefer in (first, second, second):
            self.browser.detach()
            self.driver = self.browser.open(lambda: True, prefer=prefer)
            self.assertEqual(self.driver.current_window_handle, prefer)
        self.browser.detach()
        self.driver = self.browser.open(lambda: True, prefer="closed-handle")
        self.assertEqual(self.driver.current_window_handle, self.driver.window_handles[0])
        self.wait(lambda: self.driver.execute_script("return window.wagerClicks") == 0)

    def test_a_renderer_reload_attach_returns_only_after_its_navigation_commits(self):
        # A refused command right after attach used to escape as a browser fault and force another recovery.
        self.browser.detach()
        self.browser.reload_page = True
        with patch("draftkings_login.TT_ELITE_URL", self.url+"slow-league"):
            self.driver = self.browser.open(lambda: True)
        self.assertEqual(self.driver.execute_script("return location.pathname"), "/slow-league")
        self.assertEqual(self.driver.command_guard.gate.pending, {})
        rows = [json.loads(line) for line in (self.directory/"browser-lifecycle.jsonl").read_text().splitlines()]
        settled, attached = rows[-2:]  # setUp's own fixture load settles earlier in the log.
        self.assertEqual((settled["event"], attached["event"]), ("navigation_settled", "driver_attached"))
        self.assertGreaterEqual(settled["phases_ms"]["refused_commands"], 1)
        self.assertGreaterEqual(settled["phases_ms"]["load"], 2000)

    def crash_browser(self):
        connection = websocket.create_connection(f"ws://127.0.0.1:{self.browser.port}{self.browser.endpoint}",
                                                timeout=2, suppress_origin=True)
        try:
            connection.send(json.dumps({"id": 1, "method": "Browser.crash"}))
            try:
                connection.recv()
            except (OSError, websocket.WebSocketException):
                pass
        finally:
            connection.close()

    def test_a_busy_provider_page_delays_commands_without_a_driver_recovery(self):
        # Live DK held its main thread 11-19 s in one chain (2026-10-28); a 12 s stall, longer than
        # the old 10 s timeout, must be waited out rather than poison the session.
        self.driver.execute_script("setTimeout(()=>{const end=performance.now()+12000;while(performance.now()<end){}},0)")
        time.sleep(.2)
        started = time.monotonic()
        self.assertEqual(self.driver.execute_script("return 7"), 7)
        self.assertGreater(time.monotonic()-started, 10)
        self.assertEqual(self.driver.current_url, self.url)

    def test_driver_process_loss_reattaches_same_browser_and_retains_page_without_clicks(self):
        pid = self.browser.process.pid
        self.driver.execute_script("localStorage.setItem('fixture','retained')")
        self.driver.service.process.kill()
        self.driver.service.process.wait(timeout=15)
        with self.assertRaises(WebDriverException) as lost:
            _ = self.driver.current_url
        self.assertEqual(self.browser.recovery(lost.exception), "driver_reconnect")
        self.driver = self.browser.open(lambda: True)
        self.assertEqual(self.browser.process.pid, pid)
        self.assertEqual(self.driver.current_url, self.url)
        self.assertEqual(self.driver.execute_script("return localStorage.getItem('fixture')"), "retained")
        self.assertEqual(self.driver.execute_script("return window.wagerClicks"), 0)

    @unittest.skipUnless(os.name == "nt", "Windows driver job ownership")
    def test_worker_exit_reaps_only_its_attached_driver_and_preserves_the_browser(self):
        import driver_lifecycle
        self.browser.detach()
        engine = Path(driver_lifecycle.__file__).parent
        code = ("import json,time;from selenium.webdriver.chrome.options import Options;"
                "from driver_lifecycle import create_owned_chrome;"
                "o=Options();o.page_load_strategy='none';"
                f"o.add_experimental_option('debuggerAddress','127.0.0.1:{self.browser.port}');"
                "d=create_owned_chrome(o,preserve_external_browser=True);"
                "print(json.dumps({'driver_pid':d.service.process.pid}),flush=True);time.sleep(120)")
        child = subprocess.Popen([sys.executable, "-u", "-c", code], cwd=engine,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        identity = None
        try:
            ready = queue.Queue()
            threading.Thread(target=lambda: ready.put(child.stdout.readline()), daemon=True).start()
            line = ready.get(timeout=30)
            self.assertTrue(line, "Owned fixture worker could not attach")
            identity = psutil.Process(json.loads(line)["driver_pid"])
            created = identity.create_time()
            child.kill()
            child.wait(timeout=15)
            identity.wait(timeout=10)
            self.assertEqual(self.browser.inspect()["state"], "open")
            self.driver = self.browser.open(lambda: True)
            self.assertEqual(self.driver.execute_script("return window.wagerClicks"), 0)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=15)
            child.stdout.close()
            child.stderr.close()
            if identity is not None:
                try:
                    if identity.is_running() and identity.create_time() == created:
                        identity.kill()
                except psutil.NoSuchProcess:
                    pass

    def test_abnormal_owned_browser_exit_restarts_same_profile_without_replaying_actions(self):
        profile, pid = self.browser.profile, self.browser.process.pid
        self.driver.execute_script("localStorage.setItem('fixture','crash-persistent')")
        # Chromium's fault-injection API produces an actual abnormal browser
        # exit, rather than waiting for Windows' potentially delayed external
        # TerminateProcess teardown of active I/O.
        self.crash_browser()
        self.assertNotEqual(self.browser.process.wait(timeout=30), 0)
        self.assertEqual(self.browser.recovery(InvalidSessionIdException("owned process killed for test")), "browser_restart")
        self.driver = self.browser.open(lambda: True)
        self.driver.get(self.url)
        self.wait(lambda: self.driver.execute_script("return document.readyState") == "complete")
        self.assertNotEqual(self.browser.process.pid, pid)
        self.assertEqual(self.browser.profile, profile)
        self.assertEqual(self.driver.execute_script("return window.wagerClicks"), 0)

    def test_browser_crash_after_worker_replacement_retains_abnormal_exit_evidence(self):
        previous_worker = self.browser
        pid = previous_worker.process.pid
        previous_worker.release(close=False)
        self.browser = OwnedBrowser(self.directory, ("https://example.com", "fixture"), resume=True, headless=True)
        self.driver = self.browser.open(lambda: True)
        self.crash_browser()
        self.assertNotEqual(previous_worker.process.wait(timeout=30), 0)
        self.assertEqual(self.browser.recovery(InvalidSessionIdException("adopted browser crashed")), "browser_restart")
        self.driver = self.browser.open(lambda: True)
        self.driver.get(self.url)
        self.wait(lambda: self.driver.execute_script("return document.readyState") == "complete")
        self.assertNotEqual(self.browser.process.pid, pid)
        self.assertEqual(self.driver.execute_script("return window.wagerClicks"), 0)

    def test_last_window_close_stops_and_cannot_be_automatically_reopened(self):
        self.driver.close()
        self.wait(lambda: self.browser.inspect()["state"] in {"closed", "unknown"})
        self.assertEqual(self.browser.recovery(InvalidSessionIdException("last window closed")), "closed")
        with patch.object(self.browser, "_spawn") as launch:
            with self.assertRaises(BrowserClosed):
                self.browser.open(lambda: True)
        launch.assert_not_called()

    def test_stop_then_explicit_start_reuses_durable_profile(self):
        self.driver.execute_script("localStorage.setItem('fixture','saved-after-stop')")
        profile = self.browser.profile
        process = self.browser.process
        children = psutil.Process(process.pid).children(recursive=True)
        self.browser.release(close=True)
        # Chrome child shutdown can outlast the main process; do not race profile reuse.
        process.wait(timeout=30)
        psutil.wait_procs(children, timeout=15)
        self.browser = OwnedBrowser(self.directory, ("https://example.com", "fixture"), headless=True)
        self.driver = self.browser.open(lambda: True)
        self.driver.get(self.url)
        self.wait(lambda: self.driver.execute_script("return document.readyState") == "complete")
        self.assertEqual(self.browser.profile, profile)
        self.assertEqual(self.driver.execute_script("return localStorage.getItem('fixture')"), "saved-after-stop")
        self.assertEqual(self.driver.execute_script("return window.wagerClicks"), 0)

    def test_replaced_worker_adopts_only_the_live_owned_browser_and_manual_close_blocks_resume(self):
        pid = self.browser.process.pid
        previous_worker = self.browser
        self.browser.release(close=False)
        self.browser = OwnedBrowser(self.directory, ("https://example.com", "fixture"), resume=True, headless=True)
        self.driver = self.browser.open(lambda: True)
        self.assertEqual(self.browser.process.pid, pid)
        self.assertEqual(self.driver.current_url, self.url)
        self.driver.close()
        self.wait(lambda: self.browser.inspect()["state"] in {"closed", "unknown"})
        self.browser.release(close=False)
        self.browser = OwnedBrowser(self.directory, ("https://example.com", "fixture"), resume=True, headless=True)
        with self.assertRaises(BrowserClosed):
            self.browser.open(lambda: True)
        self.browser.release(close=True)  # Explicit fixture teardown may close a no-window background process.
        previous_worker.process.wait(timeout=30)


if __name__ == "__main__":
    unittest.main()
