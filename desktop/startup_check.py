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
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

if not getattr(sys, "frozen", False):
    # Source mode: resolve the pinned engine modules from the repository root.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MARKER_TIMEOUT = 30
STAGES = ("started", "chrome_discovered", "browser_open", "permissions_ready",
          "navigated", "teardown_complete", "cleanup_verified", "success")


def emit(stage, **fields):
    print(json.dumps({"kind": "startup_check", "stage": stage, **fields}, allow_nan=False), flush=True)


def chrome_version(binary):
    try:
        result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=20)
        return (result.stdout or result.stderr).strip()[:120] or None
    except (OSError, subprocess.SubprocessError):
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


def owned_leftovers(profile):
    """Only processes carrying this run's exact temporary profile are test-owned."""
    import psutil
    from ember_browser_lifecycle import argument
    found = []
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        arguments = process.info.get("cmdline") or []
        name = (process.info.get("name") or "").lower()
        if "chrome" not in name and "chromium" not in name:
            continue
        if argument(arguments, "--user-data-dir") == str(profile):
            found.append(process)
    return found


def lifecycle_rows(data_dir):
    try:
        path = Path(data_dir)/"browser-lifecycle.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        return []


def error_reason(error):
    """Selenium keeps the CDP rejection text in .msg, never in Exception.args."""
    return str(getattr(error, "msg", error))[:120]


def grant_location_permissions_isolated(driver):
    """Run the production permission grants one call at a time.

    Same commands, origins, and permission names as
    draftkings_login.allow_location_permissions; each call is isolated so a
    rejection names its exact command and argument instead of a bare stage.
    """
    from draftkings_login import LOCATION_PERMISSION_ORIGINS, LOCATION_PERMISSION_NAMES
    info = driver.execute_cdp_cmd("Target.getTargetInfo", {})
    context = info.get("targetInfo", {}).get("browserContextId") if isinstance(info, dict) else None
    emit("permission_context", browser_context=bool(context))
    failures = []
    for origin in LOCATION_PERMISSION_ORIGINS:
        for name in LOCATION_PERMISSION_NAMES:
            permission = {"origin": origin, "permission": {"name": name}, "setting": "granted"}
            if context:
                permission["browserContextId"] = context
            try:
                driver.execute_cdp_cmd("Browser.setPermission", permission)
            except Exception as error:
                # The rejected arguments are this repository's public
                # permission-name constants, never credentials or page data.
                failures.append((origin, name, type(error).__name__, error_reason(error)))
                emit("permission_failed", command="Browser.setPermission",
                     origin=origin, permission=name, error_type=type(error).__name__,
                     reason=error_reason(error))
    if failures:
        raise RuntimeError("permissions_failed: " + "; ".join(
            f"{name}@{origin}: {kind} {reason}" for origin, name, kind, reason in failures))


def prepare_browser_execution_isolated(driver):
    import tt_market_exec as core
    driver.set_page_load_timeout(15)
    try:
        driver.execute_cdp_cmd("Emulation.setFocusEmulationEnabled", {"enabled": True})
    except Exception as error:
        emit("permission_failed", command="Emulation.setFocusEmulationEnabled",
             error_type=type(error).__name__, reason=error_reason(error))
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
        grant_location_permissions_isolated(driver)  # Production permission calls, one at a time.
        prepare_browser_execution_isolated(driver)
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
        if browser is not None:
            try:
                browser.release(close=True)  # Detach the driver, then close the owned Chrome.
            except Exception:
                pass
        emit("teardown_complete", lifecycle=lifecycle_rows(data_dir))
        for process in owned_leftovers(data_dir):  # Strictly this run's profile: terminate, then kill.
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        for attempt in range(20):
            try:
                shutil.rmtree(data_dir)
                break
            except OSError:
                if attempt == 19:
                    emit("cleanup_failed")
                    raise
                time.sleep(.2)
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
        detail = error_reason(error)
        emit("failed", failed_stage=stage["name"], error_type=type(error).__name__, detail=detail)
        return 1
    emit("success")
    return 0


if __name__ == "__main__":
    sys.exit(main())