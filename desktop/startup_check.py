"""Startup check for the packaged worker recipe: production launch path, local fixture only.

This is the CI-side twin of a human pressing Start: it drives the exact
production browser-launch and attachment path (OwnedBrowser.open ->
endpoint discovery -> ChromeDriver attachment -> permission/setup calls ->
initial navigation) inside the packaged worker's frozen environment, against
a loopback fixture page. No sportsbook, account, or wager interaction.

Output is one JSON object per line, each naming its startup stage, so a
failure on a hosted runner identifies the exact stage that broke. Lines
never carry exception text, page DOM, credentials, or absolute home paths.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import psutil

if not getattr(sys, "frozen", False):
    # Source mode: resolve the pinned engine modules from the repository root.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ember_browser_lifecycle import argument

MARKER_TIMEOUT = 30
STAGES = ("started", "chrome_discovered", "browser_open", "permission_context", "permissions_ready",
          "navigated", "teardown_complete", "cleanup_verified", "success")


def emit(stage, **fields):
    print(json.dumps({"kind": "startup_check", "stage": stage, **fields}, allow_nan=False), flush=True)


def chrome_version(binary):
    try:
        result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=20)
        version = (result.stdout or result.stderr).strip()[:120]
        if version:
            return version
    except (OSError, subprocess.SubprocessError):
        pass
    # shortcut: chrome.exe is a GUI-subsystem app and often prints nothing to
    # a pipe on Windows; read the executable's file version instead.
    if platform.system() == "Windows":
        try:
            result = subprocess.run(["powershell", "-NoProfile", "-Command",
                                      f"(Get-Item -LiteralPath '{binary}').VersionInfo.ProductVersion"],
                                     capture_output=True, text=True, timeout=20)
            return (result.stdout or "").strip()[:120] or None
        except (OSError, subprocess.SubprocessError):
            return None
    return None


def fixture_server(marker):
    class FixturePage(BaseHTTPRequestHandler):
        def do_GET(self):
            content = f"<!doctype html><title>{marker}</title><body>fixture only</body>".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *_args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", 0), FixturePage)


def chrome_process(name):
    name = name.lower()
    return ("chrome" in name or "chromium" in name) and "chromedriver" not in name


def profile_key(profile):
    return os.path.normcase(str(Path(profile).resolve()))


def owned_leftovers(profile, nonce):
    """Only processes carrying this run's exact temporary profile are test-owned."""
    if not nonce:
        return []
    found = []
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        arguments = process.info.get("cmdline") or []
        if not chrome_process(process.info.get("name") or "") or argument(arguments, "--type"):
            continue
        current = argument(arguments, "--user-data-dir")
        if (current and profile_key(current) == profile_key(profile)
                and argument(arguments, "--ember-owned-browser") == nonce):
            found.append(process)
    return found


def owned_process_snapshot(profile, nonce):
    """Capture children while their exact profile/nonce-verified parent still exists."""
    pending = owned_leftovers(profile, nonce)
    found = {}
    while pending:
        process = pending.pop()
        try:
            identity = (process.pid, process.create_time())
            if identity in found or not process.is_running():
                continue
            arguments = process.cmdline()
            current = argument(arguments, "--user-data-dir")
            marker = argument(arguments, "--ember-owned-browser")
            if (not chrome_process(process.name()) or current and profile_key(current) != profile_key(profile)
                    or marker and marker != nonce):
                continue
            found[identity] = process
            pending.extend(process.children())
        except psutil.NoSuchProcess:
            pass
    return found


def same_process(process, identity):
    try:
        return process.is_running() and (process.pid, process.create_time()) == identity
    except psutil.NoSuchProcess:
        return False


def wait_owned_processes(snapshot):
    identities = {id(process): identity for identity, process in snapshot.items()}
    alive = list(snapshot.values())
    for action, timeout in ((None, 20), ("terminate", 5), ("kill", 5)):
        alive = [process for process in alive if same_process(process, identities[id(process)])]
        if not alive:
            return
        if action:
            for process in alive:
                try:
                    getattr(process, action)()
                except psutil.NoSuchProcess:
                    pass
        _, alive = psutil.wait_procs(alive, timeout=timeout)
    if alive:
        raise RuntimeError("startup_cleanup_process_timeout")


def cleanup_owned_browser(browser):
    snapshot = {}
    try:
        snapshot = owned_process_snapshot(browser.profile, browser.nonce)
    finally:
        try:
            browser.release(close=True)
        finally:
            snapshot.update(owned_process_snapshot(browser.profile, browser.nonce))
            wait_owned_processes(snapshot)
            if browser.process is not None and any(pid == browser.process.pid for pid, _ in snapshot):
                browser.process.wait(timeout=0)


def remove_data_dir(data_dir):
    deadline = time.monotonic()+15
    while True:
        try:
            shutil.rmtree(data_dir)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise RuntimeError("startup_cleanup_directory_failed") from None
            time.sleep(.2)


def lifecycle_rows(data_dir):
    try:
        path = Path(data_dir)/"browser-lifecycle.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        return []


def permissions_and_setup(driver):
    """The real production permission/setup calls, with redacted diagnostics.

    draftkings_login.allow_location_permissions is the exact path a human
    Start presses; it is never substituted or reimplemented here.
    """
    from draftkings_login import allow_location_permissions
    info = driver.execute_cdp_cmd("Target.getTargetInfo", {})
    context = info.get("targetInfo", {}).get("browserContextId") if isinstance(info, dict) else None
    emit("permission_context", browser_context=bool(context))
    try:
        allow_location_permissions(driver)
    except Exception as error:
        emit("permission_failed", command="Browser.setPermission",
             error_type=type(error).__name__)
        raise
    import tt_market_exec as core
    try:
        core.prepare_browser_execution(driver)
    except Exception as error:
        emit("permission_failed", command="Emulation.setFocusEmulationEnabled",
             error_type=type(error).__name__)
        raise


def run(stage):
    from ember_browser_lifecycle import OwnedBrowser, chrome_binary

    marker = "ember-startup-" + uuid.uuid4().hex
    server = fixture_server(marker)
    url = f"http://127.0.0.1:{server.server_port}/"
    data_dir = Path(tempfile.mkdtemp(prefix="ember-startup-check-"))
    server_thread = browser = driver = None
    try:
        emit("started", system=platform.system().lower(), machine=platform.machine().lower(),
             frozen=bool(getattr(sys, "frozen", False)), python=sys.version.split()[0])
        binary = chrome_binary()
        emit("chrome_discovered", chrome=Path(binary).name, chrome_version=chrome_version(binary))
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        stage["name"] = "browser_open"
        browser = OwnedBrowser(data_dir, (url, "startup-check"))
        driver = browser.open(lambda: True)  # Chrome launch -> endpoint discovery -> ChromeDriver attach.
        emit("browser_open", port=browser.port, auto_port=browser.auto_port, profile=browser.profile.name)
        stage["name"] = "permissions_ready"
        permissions_and_setup(driver)  # Real production permission/setup path.
        emit("permissions_ready")
        stage["name"] = "navigated"
        previous = driver.current_url
        driver.get(url)  # Production initial navigation away from about:blank.
        title = ""
        deadline = time.monotonic()+MARKER_TIMEOUT
        while time.monotonic() < deadline:
            try:
                title = driver.title or ""
            except Exception:
                title = ""
            if marker in title:
                break
            time.sleep(.2)
        if marker not in title:
            raise RuntimeError("startup_marker_timeout")
        if driver.current_url == previous or not driver.current_url.startswith(url):
            raise RuntimeError("startup_navigation_unchanged")
        emit("navigated", previous_url=previous, url=driver.current_url, marker_observed=True)
        stage["name"] = "browser_closed"
    finally:
        body_failed = sys.exc_info()[0] is not None
        cleanup_stage = "teardown_complete"
        try:
            if browser is not None:
                cleanup_owned_browser(browser)
            emit("teardown_complete", lifecycle=lifecycle_rows(data_dir))
            cleanup_stage = "cleanup_verified"
            if not body_failed:
                stage["name"] = cleanup_stage
            remove_data_dir(data_dir)
        except Exception as error:
            stage["name"] = cleanup_stage
            emit("cleanup_failed", error_type=type(error).__name__)
            raise
        finally:
            if server_thread is not None:
                server.shutdown()
            server.server_close()
            if server_thread is not None:
                server_thread.join(timeout=5)
    emit("cleanup_verified")


def main():
    stage = {"name": "started"}
    try:
        run(stage)
    except Exception as error:
        emit("failed", failed_stage=stage["name"], error_type=type(error).__name__)
        return 1
    emit("success")
    return 0


if __name__ == "__main__":
    sys.exit(main())
