"""Startup-check ownership, bounded cleanup and redaction; no browser or network."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

import psutil

from desktop import startup_check as check


PROFILE = Path("startup-fixture")/"browser-profiles"/"account"/"chrome"
NONCE = "a"*48
SECRET = r"C:\Users\Private Home\fixture-password\chrome\Default\DIPS"


def process(pid, *, profile=PROFILE, nonce=NONCE, name="chrome.exe", child=False):
    arguments = []
    if profile is not None:
        arguments.append("--user-data-dir="+str(profile))
    if nonce is not None:
        arguments.append("--ember-owned-browser="+nonce)
    if child:
        arguments.append("--type=renderer")
    value = Mock(pid=pid, info={"pid": pid, "name": name, "cmdline": arguments})
    value.name.return_value = name
    value.cmdline.return_value = arguments
    value.create_time.return_value = float(pid)
    value.is_running.return_value = True
    value.children.return_value = []
    return value


class OwnedCleanupTests(unittest.TestCase):
    def test_only_exact_profile_and_nonce_chrome_roots_are_owned(self):
        owned = process(1)
        rejected = [process(2, profile=PROFILE.parent), process(3, profile=Path(str(PROFILE)+"-other")),
                    process(4, nonce="b"*48), process(5, nonce=None), process(6, child=True),
                    process(7, name="chromedriver.exe"), process(8, name="python.exe")]
        with patch.object(check.psutil, "process_iter", return_value=[owned, *rejected]):
            self.assertEqual(check.owned_leftovers(PROFILE, NONCE), [owned])
            self.assertEqual(check.owned_leftovers(PROFILE.parent.parent.parent, NONCE), [])
            self.assertEqual(check.owned_leftovers(PROFILE, None), [])
        for value in rejected:
            value.terminate.assert_not_called()
            value.kill.assert_not_called()

    def test_snapshot_includes_owned_descendants_without_profile_arguments(self):
        root = process(1)
        child = process(2, profile=None, nonce=None, child=True)
        grandchild = process(3, nonce=None, child=True)
        foreign = process(4, profile=PROFILE.parent/"unrelated")
        unowned = process(5, name="python.exe", profile=None, nonce=None)
        root.children.return_value = [child, foreign, unowned]
        child.children.return_value = [grandchild]
        with patch.object(check.psutil, "process_iter", return_value=[root, foreign]):
            snapshot = check.owned_process_snapshot(PROFILE, NONCE)
        self.assertEqual(set(snapshot), {(1, 1.0), (2, 2.0), (3, 3.0)})
        foreign.children.assert_not_called()
        unowned.children.assert_not_called()

    def test_children_are_captured_before_release_and_awaited_after_parent_exits(self):
        root = process(1)
        child = process(2, profile=None, nonce=None, child=True)
        unrelated = process(3, profile=PROFILE.parent/"unrelated")
        root.children.return_value = [child]
        browser = SimpleNamespace(profile=PROFILE, nonce=NONCE, process=Mock(pid=1), release=Mock())
        def release(**kwargs):
            root.children.assert_called_once_with()
            root.children.return_value = []
            root.is_running.return_value = False
        browser.release.side_effect = release
        with patch.object(check.psutil, "process_iter", return_value=[root, unrelated]), \
                patch.object(check.psutil, "wait_procs", return_value=([child], [])) as wait:
            check.cleanup_owned_browser(browser)
        browser.release.assert_called_once_with(close=True)
        wait.assert_called_once_with([child], timeout=20)
        browser.process.wait.assert_called_once_with(timeout=0)
        child.terminate.assert_not_called()
        unrelated.terminate.assert_not_called()
        unrelated.kill.assert_not_called()

    def test_bounded_escalation_only_signals_snapshotted_owned_processes(self):
        child = process(2, profile=None, nonce=None, child=True)
        with patch.object(check.psutil, "wait_procs", side_effect=[([], [child]), ([child], [])]) as wait:
            check.wait_owned_processes({(2, 2.0): child})
        self.assertEqual(wait.call_args_list, [call([child], timeout=20), call([child], timeout=5)])
        child.terminate.assert_called_once_with()
        child.kill.assert_not_called()

    def test_release_failure_does_not_skip_waiting_for_captured_children(self):
        root = process(1)
        child = process(2, profile=None, nonce=None, child=True)
        root.children.return_value = [child]
        browser = SimpleNamespace(profile=PROFILE, nonce=NONCE, process=Mock(pid=1),
                                  release=Mock(side_effect=RuntimeError(SECRET)))
        with patch.object(check.psutil, "process_iter", return_value=[root]), \
                patch.object(check.psutil, "wait_procs", return_value=([root, child], [])) as wait:
            with self.assertRaises(RuntimeError):
                check.cleanup_owned_browser(browser)
        self.assertEqual(set(wait.call_args.args[0]), {root, child})
        browser.process.wait.assert_called_once_with(timeout=0)

    def test_pid_reuse_during_grace_never_signals_the_replacement(self):
        child = process(2, profile=None, nonce=None, child=True)
        def reused(*args, **kwargs):
            child.create_time.return_value = 999.0
            return [], [child]
        with patch.object(check.psutil, "wait_procs", side_effect=reused) as wait:
            check.wait_owned_processes({(2, 2.0): child})
        wait.assert_called_once()
        child.terminate.assert_not_called()
        child.kill.assert_not_called()

    def test_ownership_that_cannot_be_reverified_fails_without_signalling(self):
        child = process(2)
        child.create_time.side_effect = psutil.AccessDenied(2)
        with self.assertRaises(psutil.AccessDenied):
            check.wait_owned_processes({(2, 2.0): child})
        child.terminate.assert_not_called()
        child.kill.assert_not_called()

    def test_surviving_process_is_a_failure_after_bounded_waits(self):
        child = process(2)
        with patch.object(check.psutil, "wait_procs", return_value=([], [child])) as wait:
            with self.assertRaisesRegex(RuntimeError, "^startup_cleanup_process_timeout$"):
                check.wait_owned_processes({(2, 2.0): child})
        self.assertEqual([item.kwargs["timeout"] for item in wait.call_args_list], [20, 5, 5])
        child.terminate.assert_called_once_with()
        child.kill.assert_called_once_with()


class RedactionTests(unittest.TestCase):
    def test_directory_cleanup_waits_for_file_release_and_redacts_terminal_errors(self):
        with patch.object(check.shutil, "rmtree", side_effect=[PermissionError(SECRET), None]) as remove, \
                patch.object(check.time, "sleep") as sleep:
            check.remove_data_dir(PROFILE)
        self.assertEqual(remove.call_count, 2)
        sleep.assert_called_once_with(.2)
        with patch.object(check.shutil, "rmtree", side_effect=PermissionError(SECRET)), \
                patch.object(check.time, "monotonic", side_effect=[0, 15]):
            with self.assertRaises(RuntimeError) as failure:
                check.remove_data_dir(PROFILE)
        self.assertEqual(str(failure.exception), "startup_cleanup_directory_failed")
        self.assertNotIn(SECRET, str(failure.exception))

    def test_main_never_emits_exception_text_or_home_paths(self):
        output = io.StringIO()
        with patch.object(check, "run", side_effect=PermissionError(SECRET)), redirect_stdout(output):
            self.assertEqual(check.main(), 1)
        self.assertEqual(json.loads(output.getvalue()), {
            "kind": "startup_check", "stage": "failed", "failed_stage": "started", "error_type": "PermissionError"})
        self.assertNotIn(SECRET, output.getvalue())
        self.assertNotIn("Private Home", output.getvalue())

    def test_permission_failure_reports_command_and_type_without_exception_text(self):
        driver = Mock()
        driver.execute_cdp_cmd.return_value = {"targetInfo": {"browserContextId": SECRET}}
        output = io.StringIO()
        with patch("draftkings_login.allow_location_permissions", side_effect=PermissionError(SECRET)), \
                redirect_stdout(output), self.assertRaises(PermissionError):
            check.permissions_and_setup(driver)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(rows[-1], {"kind": "startup_check", "stage": "permission_failed",
                                  "command": "Browser.setPermission", "error_type": "PermissionError"})
        self.assertNotIn(SECRET, output.getvalue())


if __name__ == "__main__":
    unittest.main()
