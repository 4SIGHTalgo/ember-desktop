"""Startup smoke gate: the packaged worker starts, and the production
browser-launch/attachment path reaches a harmless local page.

The worker bundle and a startup-check bundle are built with the exact pinned
PyInstaller recipe the release uses (desktop/build_worker.py). The check
drives OwnedBrowser.open (Chrome launch -> endpoint discovery ->
ChromeDriver attach), the production permission/setup calls, and the initial
navigation against a loopback fixture with a per-run marker. No sportsbook,
account, or wager interaction. Run explicitly on the native build hosts
(Windows, Intel macOS, Apple Silicon); not part of root test discovery.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import desktop.build_worker as build_worker
from ember_protocol import VERSION
from desktop.startup_check import STAGES

ARCH = {"x86_64-pc-windows-msvc": "amd64", "x86_64-apple-darwin": "x86_64",
        "aarch64-apple-darwin": "arm64"}
WORKER_TIMEOUT = 240
CHECK_TIMEOUT = 420


def clean_environment(home=None):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("EMBER_")}
    if home is not None:
        environment["LOCALAPPDATA"] = str(home)
        environment["HOME"] = str(home)
    return environment


class StartupSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binaries = build_worker.binary_dir
        cls.worker = cls.binaries/("ember-worker-" + build_worker.target + build_worker.suffix)
        if not cls.worker.exists():
            build_worker.build(build_worker.ENGINE, build_worker.ENGINE/"ember_client.py",
                               "ember-worker-" + build_worker.target)
        cls.startup = cls.binaries/("ember-startup-" + build_worker.target + build_worker.suffix)
        build_worker.build(build_worker.ENGINE, ROOT/"desktop/startup_check.py",
                           "ember-startup-" + build_worker.target)

    def parse(self, stdout):
        rows = []
        for line in stdout.splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    self.fail("Non-JSON output line: " + line[:400])
        return rows

    def run_check(self, command, timeout):
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                                env=clean_environment())
        self.assertEqual(result.returncode, 0,
                         msg="\n".join([result.stdout, result.stderr])[-4000:])
        rows = [row for row in self.parse(result.stdout) if row.get("kind") == "startup_check"]
        stages = [row["stage"] for row in rows]
        self.assertEqual(stages, list(STAGES), msg=result.stdout[-4000:])
        return {row["stage"]: row for row in rows}

    def assert_startup(self, stages, frozen):
        self.assertEqual(stages["started"]["machine"], ARCH[build_worker.target])
        self.assertEqual(stages["started"]["frozen"], frozen)
        self.assertTrue(stages["chrome_discovered"].get("chrome_version"))
        navigated = stages["navigated"]
        self.assertTrue(navigated["marker_observed"])
        self.assertTrue(navigated["url"].startswith("http://127.0.0.1:"))
        self.assertNotEqual(navigated["url"], navigated["previous_url"])
        self.assertIn("browser_launch", [row.get("event") for row in stages["teardown_complete"]["lifecycle"]])

    def test_packaged_worker_reports_ready_and_stops_cleanly(self):
        home = Path(tempfile.mkdtemp(prefix="ember-worker-ready-"))
        try:
            result = subprocess.run([str(self.worker)], input="", capture_output=True, text=True,
                                    timeout=WORKER_TIMEOUT, env=clean_environment(home))
        finally:
            shutil.rmtree(home, ignore_errors=True)
        self.assertEqual(result.returncode, 0, msg=result.stderr[-4000:])
        rows = self.parse(result.stdout)
        kinds = [row["kind"] for row in rows]
        self.assertIn("ready", kinds, msg=result.stdout[-4000:])
        self.assertIn("stopped", kinds)
        self.assertEqual(next(row for row in rows if row["kind"] == "ready")["version"], VERSION)
        self.assertNotIn("Traceback", result.stderr)
        for row in rows:  # The worker never held credentials; no key, token, or home path may appear.
            text = json.dumps(row)
            self.assertNotIn(str(home), text)
            self.assertNotIn("password", text.lower())

    def test_frozen_startup_reaches_the_local_fixture(self):
        stages = self.run_check([str(self.startup)], CHECK_TIMEOUT)
        self.assert_startup(stages, frozen=True)

    def test_source_startup_reaches_the_local_fixture(self):
        stages = self.run_check([sys.executable, str(ROOT/"desktop/startup_check.py")], CHECK_TIMEOUT)
        self.assert_startup(stages, frozen=False)


if __name__ == "__main__":
    unittest.main()