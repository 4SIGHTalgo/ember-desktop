"""Desktop-owned persistent Chrome, disposable drivers, and explicit recovery intent.

No WebDriver command is retried here. Browser/driver recovery is separate from
execution; callers must discard old signals and revalidate all permission.
"""
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

import psutil
import requests
import websocket
from selenium.webdriver.chrome.options import Options

from driver_lifecycle import close_owned_driver, create_owned_chrome


class BrowserClosed(RuntimeError):
    """Clean/possible human close: only a new explicit Start may reopen Chrome."""


class BrowserCancelled(RuntimeError):
    """Stop/quit won the race with startup or recovery."""


class BrowserWaiting(RuntimeError):
    """Ownership, startup, or endpoint is not ready; do not kill a live browser."""


class ProcessExitWitness:
    """Retain exit evidence even after an adopted, non-child browser disappears."""
    def __init__(self, pid):
        self.kernel = self.handle = self.events = None
        self.result = None
        try:
            if os.name == "nt":
                self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                self.kernel.OpenProcess.restype = wintypes.HANDLE
                self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                self.kernel.WaitForSingleObject.restype = wintypes.DWORD
                self.kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
                self.kernel.GetExitCodeProcess.restype = wintypes.BOOL
                self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                self.kernel.CloseHandle.restype = wintypes.BOOL
                self.handle = self.kernel.OpenProcess(0x101000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION.
            elif sys.platform == "darwin":
                import select
                self.events = select.kqueue()
                # NOTE_EXITSTATUS is defined by Apple's public sys/event.h,
                # including for a non-child process the caller may signal.
                event = select.kevent(pid, filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                    fflags=select.KQ_NOTE_EXIT | 0x04000000)
                self.events.control([event], 0, 0)
        except OSError:
            self.close()

    def poll(self):
        if self.result is not None:
            return self.result
        try:
            if self.handle:
                state = self.kernel.WaitForSingleObject(self.handle, 0)
                if state == 258:  # WAIT_TIMEOUT, not an exit code of 259.
                    return False, None
                if state == 0:
                    code = wintypes.DWORD()
                    known = self.kernel.GetExitCodeProcess(self.handle, ctypes.byref(code))
                    self.result = (True, int(code.value) if known else None)
                    return self.result
            elif self.events is not None:
                events = self.events.control(None, 1, 0)
                if not events:
                    return False, None
                event = events[0]
                # Unknown/missing status remains unknown, never a guessed crash.
                code = os.waitstatus_to_exitcode(event.data & 0xffff) if event.fflags & 0x04000000 else None
                self.result = (True, code)
                return self.result
        except (OSError, ValueError):
            pass
        return None

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
        if self.events is not None:
            self.events.close()
            self.events = None


class ProfileLease:
    def __init__(self, path):
        self.path, self.file = Path(path), None

    def acquire(self):
        if self.file is not None:
            return
        stream = self.path.open("a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise BrowserWaiting("browser_profile_in_use") from None
        self.file = stream

    def release(self):
        if self.file is not None:
            self.file.close()  # Closing releases the OS lease, including after a crash.
            self.file = None


def startup_stage(error):
    """The exact wait-stage code of a browser startup failure, else None."""
    if isinstance(error, BrowserWaiting) and error.args and isinstance(error.args[0], str):
        return error.args[0]
    return None


class LifecycleLog:
    """Small local-only diagnostic ring. Never accepts exception text or DOM."""
    def __init__(self, path):
        self.path = Path(path)

    def record(self, event, *, error=None, exit_code=None, attempt=None, stage=None):
        row = {"at": datetime.now(timezone.utc).isoformat(), "event": event[:60]}
        if error is not None:
            row["error_type"] = type(error).__name__[:80]
        if type(exit_code) is int:
            row["exit_code"] = exit_code
        if type(attempt) is int:
            row["attempt"] = max(0, min(attempt, 1_000_000))
        if stage is not None:
            row["stage"] = stage[:60]
        try:
            if self.path.exists() and self.path.stat().st_size > 256_000:
                os.replace(self.path, self.path.with_suffix(".previous.jsonl"))
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, allow_nan=False)+"\n")
        except OSError:
            pass  # Diagnostic storage cannot stop a healthy browser.


class ActiveSessionPower:
    """Prevent idle system sleep only while this execution thread is active."""
    def __init__(self):
        self.process, self.release_windows = None, None

    def acquire(self):
        try:
            if os.name == "nt":
                function = ctypes.WinDLL("kernel32", use_last_error=True).SetThreadExecutionState
                function.argtypes, function.restype = [ctypes.c_uint32], ctypes.c_uint32
                if function(0x80000001):  # CONTINUOUS | SYSTEM_REQUIRED; display may sleep.
                    self.release_windows = function
                    return True
            elif sys.platform == "darwin":
                self.process = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
        except OSError:
            pass
        return False

    def release(self):
        if self.release_windows is not None:
            self.release_windows(0x80000000)
            self.release_windows = None
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
            self.process = None


def chrome_binary():
    candidates = []
    if os.name == "nt":
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe") as key:
                    candidates.append(winreg.QueryValueEx(key, "")[0])
            except OSError:
                pass
        for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            if os.environ.get(variable):
                candidates.append(str(Path(os.environ[variable])/"Google/Chrome/Application/chrome.exe"))
    elif sys.platform == "darwin":
        candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                      str(Path.home()/"Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    candidates.extend(shutil.which(name) for name in ("google-chrome", "chrome", "chromium", "chromium-browser"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise BrowserWaiting("chrome_not_installed")


def argument(arguments, name):
    for index, value in enumerate(arguments):
        if value.startswith(name+"="):
            return value[len(name)+1:].strip('"')
        if value == name and index+1 < len(arguments):
            return arguments[index+1].strip('"')
    return None


class OwnedBrowser:
    def __init__(self, data_dir, scope, *, ticket=None, resume=False, headless=False, binary=None,
                 clock=time.monotonic):
        self.data_dir, self.clock = Path(data_dir).resolve(), clock
        if not scope or not all(isinstance(item, str) and item for item in scope):
            raise ValueError("A signed-in API/account scope is required for the browser profile")
        key = hashlib.sha256(json.dumps(scope, separators=(",", ":")).encode()).hexdigest()[:32]
        self.retained = ticket is not None
        self.folder = self.data_dir/"browser-profiles"/("validation" if ticket else key)
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.folder.resolve().is_relative_to(self.data_dir):
            raise ValueError("Browser storage must remain inside the app data directory")
        self.profile = (self.data_dir/"validation-browser") if ticket else self.folder/"chrome"
        if ticket and Path(ticket["profile"]).resolve() != self.profile.resolve():
            raise ValueError("Only the dedicated validation profile can be retained")
        self.profile.mkdir(mode=0o700, exist_ok=True)
        if self.profile.is_symlink() or not self.profile.resolve().is_relative_to(self.data_dir):
            raise ValueError("Browser profile must remain inside the app data directory")
        self.lease = ProfileLease(self.folder/"owner.lock")
        self.log = LifecycleLog(self.data_dir/"browser-lifecycle.jsonl")
        self.state_path = self.folder/"browser.json"
        self.session = requests.Session()
        self.session.trust_env = False  # A system proxy must never receive local CDP traffic.
        self.process = self.driver = None
        self.exit_witness = None
        self.driver_identity = self.stale_driver = None
        self.created = None
        self.port = ticket["port"] if ticket else None
        self.auto_port = not self.retained
        self.endpoint = self.nonce = None
        self.headless, self.binary = headless, binary
        self.resume, self.initialized, self.closed = resume, False, False
        self.ever_open = False
        self.launched_at = None
        self.reload_page = False
        self.exit_code = None

    def _state(self, **extra):
        row = {"protocol": 1, "nonce": self.nonce, "pid": self.process.pid if self.process else None,
                "created": self.created, "port": self.port, "endpoint": self.endpoint,
               "driver": self.driver_identity, "auto_port": self.auto_port, **extra}
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(row, allow_nan=False), encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, self.state_path)

    def _owners(self):
        found, other = [], False
        for process in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
            info = process.info
            arguments = info.get("cmdline") or []
            if argument(arguments, "--type") in {"renderer", "gpu-process", "utility", "zygote", "broker", "crashpad-handler"}:
                continue  # Crash teardown helpers are not competing profile owners.
            profile = argument(arguments, "--user-data-dir")
            if not profile or os.path.normcase(str(Path(profile).resolve())) != os.path.normcase(str(self.profile.resolve())):
                continue
            if "chrome" not in (info.get("name") or "").lower() and "chromium" not in (info.get("name") or "").lower():
                other = True
                continue
            address = argument(arguments, "--remote-debugging-address")
            port = argument(arguments, "--remote-debugging-port")
            valid_port = port == str(self.port) if self.retained else (port == "0" or (self.port is not None and port == str(self.port)))
            owned = address in (None, "127.0.0.1", "localhost") and valid_port
            owned = owned and (self.retained or argument(arguments, "--ember-owned-browser") == self.nonce)
            if owned:
                found.append(process)
            else:
                other = True
        if len(found) > 1 or (other and not found):
            raise BrowserWaiting("browser_profile_in_use")
        return found

    def _restore(self):
        if self.state_path.exists():
            try:
                if self.state_path.stat().st_size > 8192:
                    raise ValueError("Invalid ownership record")
                row = json.loads(self.state_path.read_text(encoding="utf-8"))
                if not self.retained:
                    nonce = row.get("nonce")
                    if not isinstance(nonce, str) or len(nonce) != 48 or any(c not in "0123456789abcdef" for c in nonce):
                        raise ValueError("Invalid ownership marker")
                    self.nonce, self.port, self.endpoint = nonce, row.get("port"), row.get("endpoint")
                cached = row.get("driver")
                if (isinstance(cached, dict) and type(cached.get("pid")) is int and cached["pid"] > 0
                        and type(cached.get("created")) in (int, float) and math.isfinite(cached["created"])
                        and type(cached.get("port")) is int and 0 < cached["port"] <= 65535):
                    self.stale_driver = cached
            except (OSError, ValueError, TypeError):
                raise BrowserWaiting("browser_ownership_unavailable") from None
        owners = self._owners()
        self._dispose_stale_driver()
        if owners:
            self.process = owners[0]
            self.created = self.process.create_time()
            self.auto_port = argument(self.process.cmdline(), "--remote-debugging-port") == "0"
            self.exit_witness = ProcessExitWitness(self.process.pid)
        elif self.retained or self.resume:
            # A resumed worker is not a new human Start. With no live owned
            # browser, lost intent/exit evidence must not override a possible X.
            raise BrowserClosed("browser_closed_while_worker_unavailable")
        self.initialized = True

    def _dispose_stale_driver(self):
        if not self.stale_driver:
            return
        cached, self.stale_driver = self.stale_driver, None
        try:
            driver = psutil.Process(cached["pid"])
            if (driver.create_time() != cached["created"] or "chromedriver" not in driver.name().lower()
                    or argument(driver.cmdline(), "--port") != str(cached["port"])):
                return  # PID reuse or a different process must never be killed.
            driver.kill()  # Only this journaled driver, never Chrome or a process tree.
            try:
                driver.wait(timeout=5)
            except psutil.TimeoutExpired:
                pass
            self.log.record("orphan_driver_released")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    def _spawn(self, active):
        if not active():
            raise BrowserCancelled()
        if self._owners():
            raise BrowserWaiting("browser_owner_changed")
        self.nonce = secrets.token_hex(24)
        self.port = self.endpoint = self.exit_code = None
        # Preserve the pre-0.1.7 native Windows launch behavior. Chrome's port 0
        # switch also enables its automation-marked startup mode, even when the
        # user signs in manually in a standalone headed browser.
        self.auto_port = os.name != "nt" or self.headless
        if not self.auto_port:
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                self.port = reservation.getsockname()[1]
        self._state()
        (self.profile/"DevToolsActivePort").unlink(missing_ok=True)
        arguments = [self.binary or chrome_binary(), f"--user-data-dir={self.profile}", "--profile-directory=Default",
                     "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={0 if self.auto_port else self.port}",
                     f"--ember-owned-browser={self.nonce}", "--no-first-run", "--no-default-browser-check",
                     "--disable-background-mode", "--new-window", "about:blank"]
        if self.headless:
            arguments[1:1] = ["--headless=new", "--window-size=1280,900"]
        self.log.record("browser_launch")
        self.launched_at = self.clock()
        self.process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            start_new_session=os.name != "nt")
        self.created = psutil.Process(self.process.pid).create_time()
        self._state()

    def _exit(self):
        if self.process is None:
            return "absent", None
        if isinstance(self.process, subprocess.Popen):
            code = self.process.poll()
            if code is None:
                return "alive", None
        else:
            try:
                witnessed = self.exit_witness.poll() if self.exit_witness else None
                if witnessed is not None:
                    exited, code = witnessed
                    if not exited:
                        return "alive", None
                else:
                    if self.process.create_time() != self.created:
                        return "unknown", None
                    code = self.process.wait(timeout=0)
            except psutil.TimeoutExpired:
                return "alive", None
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return "unknown", None
        self.exit_code = code
        return ("closed" if code == 0 else "crashed" if type(code) is int else "unknown"), code

    def _json(self, path):
        response = self.session.get(f"http://127.0.0.1:{self.port}"+path, timeout=(.5, .7), allow_redirects=False)
        if response.status_code != 200 or len(response.content) > 256_000:
            raise BrowserWaiting("browser_endpoint_unavailable")
        return response.json()

    def _owns_listener(self):
        """A selected port must belong to this exact Chrome, not a bind-race peer."""
        try:
            listeners = [connection for connection in psutil.net_connections(kind="tcp")
                if connection.status == psutil.CONN_LISTEN and connection.laddr.port == self.port]
            return bool(listeners) and all(connection.pid == self.process.pid
                and connection.laddr.ip in {"127.0.0.1", "::1"} for connection in listeners)
        except (psutil.Error, OSError):
            return False

    def inspect(self):
        state, code = self._exit()
        if state != "alive":
            return {"state": state, "exit_code": code}
        try:
            if not self.retained:
                if self.auto_port:
                    lines = (self.profile/"DevToolsActivePort").read_text(encoding="utf-8").splitlines()
                    if len(lines) != 2 or not lines[0].isdigit() or not 1024 <= int(lines[0]) <= 65535:
                        raise ValueError("Invalid browser endpoint")
                    if self.endpoint is not None and lines[1] != self.endpoint:
                        raise BrowserWaiting("browser_endpoint_changed")
                    self.port = int(lines[0])
                elif not self._owns_listener():
                    raise BrowserWaiting("browser_listener_unverified")
            version = self._json("/json/version")
            endpoint = urlsplit(version["webSocketDebuggerUrl"])
            if (endpoint.scheme != "ws" or endpoint.hostname not in {"127.0.0.1", "localhost"}
                    or endpoint.port != self.port or endpoint.username or endpoint.query or endpoint.fragment
                    or not endpoint.path.startswith("/devtools/browser/")):
                raise ValueError("Invalid browser endpoint")
            if self.endpoint is not None and endpoint.path != self.endpoint:
                raise BrowserWaiting("browser_endpoint_changed")
            self.endpoint = endpoint.path
            pages = self._json("/json/list")
            if not isinstance(pages, list):
                raise ValueError("Invalid browser targets")
            count = sum(item.get("type") == "page" and not str(item.get("url", "")).startswith("devtools://")
                        for item in pages if isinstance(item, dict))
            if count:
                self.ever_open = True
            if not count and not self.ever_open and self.launched_at is not None and self.clock()-self.launched_at < 3:
                return {"state": "waiting"}  # A just-launched process may not have its initial tab yet.
            return {"state": "open" if count else "closed", "pages": count}
        except (OSError, ValueError, KeyError, TypeError, requests.RequestException, BrowserWaiting):
            return {"state": "waiting"}

    def open(self, active):
        if self.closed:
            raise BrowserClosed("browser_closed")
        if not active():
            raise BrowserCancelled()
        self.lease.acquire()
        if not self.initialized:
            self._restore()
        if self.process is None:
            self._spawn(active)
        deadline = self.clock()+20
        while active():
            inspection = self.inspect()
            if inspection["state"] == "open":
                break
            # Some Chrome builds use a short-lived launcher process. Adopt only
            # its exact ownership-marked browser, never another profile/process.
            if inspection["state"] != "waiting":
                owners = self._owners()
                if owners and owners[0].pid != self.process.pid:
                    if self.exit_witness:
                        self.exit_witness.close()
                    self.process, self.created = owners[0], owners[0].create_time()
                    self.auto_port = argument(self.process.cmdline(), "--remote-debugging-port") == "0"
                    self.exit_witness = ProcessExitWitness(self.process.pid)
                    continue
                if inspection["state"] in {"closed", "unknown"}:
                    self.closed = True
                    raise BrowserClosed("browser_closed")
                raise BrowserWaiting("browser_start_failed")
            if self.clock() >= deadline:
                raise BrowserWaiting("browser_endpoint_unavailable")
            time.sleep(.1)
        else:
            raise BrowserCancelled()
        self._state()
        options = Options()
        options.page_load_strategy = "none"
        options.add_experimental_option("debuggerAddress", f"127.0.0.1:{self.port}")
        # debuggerAddress attaches to our standalone process. Current ChromeDriver
        # rejects launch-only `detach` in this attachment mode; service disposal
        # below deliberately never sends session quit/Browser.close instead.
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
        self.driver = create_owned_chrome(options, preserve_external_browser=True)
        self.driver_identity = {"pid": self.driver.service.process.pid,
            "created": psutil.Process(self.driver.service.process.pid).create_time(), "port": self.driver.service.port}
        self._state()
        self.driver.command_executor.client_config.timeout = 5
        if not active():
            self.detach()
            raise BrowserCancelled()
        handles = self.driver.window_handles
        if not handles:
            self.detach()
            self.closed = True
            raise BrowserClosed("browser_windows_closed")
        # A replacement WebDriver session must select a current handle instead
        # of retaining an already-destroyed default target from attachment.
        self.driver.switch_to.window(handles[0])
        if self.reload_page:
            from draftkings_login import LOGIN_URL
            self.driver.get(LOGIN_URL)  # Safe GET after renderer crash; never replay a script/click.
            self.reload_page = False
        self.log.record("driver_attached")
        return self.driver

    def detach(self):
        if self.driver is not None:
            try:
                close_owned_driver(self.driver)
            except Exception:
                pass  # Disposal cannot turn a recoverable driver fault into Browser.close.
            self.driver = None
            self.driver_identity = None
            try:
                self._state()
            except OSError:
                pass

    def recovery(self, error):
        self.detach()
        self.log.record("browser_fault", error=error, stage=startup_stage(error))
        if self.closed or isinstance(error, BrowserClosed):
            state = {"state": "closed"}
        else:
            state = self.inspect()
        if state["state"] in {"closed", "unknown"} or (self.retained and state["state"] in {"absent", "crashed"}):
            self.closed = True
            self.log.record("browser_clean_or_unknown_close", exit_code=state.get("exit_code"))
            return "closed"
        if state["state"] == "crashed":
            self.log.record("browser_abnormal_exit", exit_code=state.get("exit_code"))
            if self.exit_witness:
                self.exit_witness.close()
                self.exit_witness = None
            self.process = None
            self.port = self.endpoint = None
            return "browser_restart"
        if state["state"] == "open":
            self.reload_page = "tab crashed" in str(error).lower()
            return "renderer_reload" if self.reload_page else "driver_reconnect"
        return "browser_wait"

    def _close_browser(self):
        if self.retained or self.process is None:
            return
        try:
            try:
                children = psutil.Process(self.process.pid).children(recursive=True)
            except psutil.Error:
                children = []
            inspection = self.inspect()
            if inspection["state"] == "open" or inspection.get("pages") == 0:
                connection = websocket.create_connection(f"ws://127.0.0.1:{self.port}{self.endpoint}",
                    timeout=1, suppress_origin=True, enable_multithread=False)
                try:
                    connection.send(json.dumps({"id": 1, "method": "Browser.close"}))
                finally:
                    connection.close()
            self.process.wait(timeout=3)
            psutil.wait_procs(children, timeout=2)
        except (OSError, psutil.Error, subprocess.TimeoutExpired, websocket.WebSocketException):
            # Only explicit Stop/quit reaches this path, and only this exact
            # ownership-validated process may be terminated. Never a process tree.
            try:
                owners = self._owners()
                if any(owner.pid == self.process.pid and owner.create_time() == self.created for owner in owners):
                    self.process.terminate()
                    self.process.wait(timeout=3)
            except (OSError, psutil.Error, subprocess.TimeoutExpired, BrowserWaiting):
                pass

    def release(self, *, close=False):
        try:
            self.detach()
            if close:
                self._close_browser()
            self.log.record("session_stopped" if close else "browser_preserved")
        finally:
            self.session.close()
            if self.exit_witness:
                self.exit_witness.close()
                self.exit_witness = None
            self.lease.release()
