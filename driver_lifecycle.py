"""One bot runner owns one isolated ChromeDriver service for its full lifetime."""

import ctypes
from ctypes import wintypes
import os
import subprocess
import sys
import time

from selenium import webdriver
from selenium.common.exceptions import InvalidSessionIdException, NoSuchWindowException, WebDriverException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.remote.webdriver import WebDriver as RemoteWebDriver
from urllib3.exceptions import HTTPError


BOT_TERMINAL_TITLE = "SPORTSBOOK BOT - KEEP THIS TERMINAL OPEN"


class DriverConnectionLost(WebDriverException):
    """The local driver connection is gone; no wager command may be replayed."""


def connection_lost(error):
    if isinstance(error, (DriverConnectionLost, InvalidSessionIdException, NoSuchWindowException)):
        return True
    return isinstance(error, WebDriverException) and any(text in str(error).lower() for text in (
        "chrome not reachable", "not connected to devtools", "disconnected from chrome",
        "target window already closed", "web view not found", "tab crashed"))


class _WindowsDriverJob:
    """A non-inherited job handle kills its driver if the runner is terminated."""

    def __init__(self):
        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("minimum_working_set", ctypes.c_size_t),
                        ("maximum_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("reads", "writes", "other", "read_bytes", "write_bytes", "other_bytes")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", IoCounters),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                       ctypes.c_void_p, wintypes.DWORD]
        self.kernel.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class OwnedChromeService(Service):
    """Driver child belongs to this runner, never to an older console."""

    def __init__(self, executable_path=None, preserve_external_browser=False):
        self.owner_pid = os.getpid()
        self.preserve_external_browser = preserve_external_browser
        self._owner_job = None
        self.process = None
        super().__init__(executable_path=executable_path,
                         popen_kw={"creation_flags": subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0})

    def _start_process(self, path):
        self._owner_job = _WindowsDriverJob() if os.name == "nt" else None
        try:
            super()._start_process(path)
            if self._owner_job is not None:
                self._owner_job.assign(self.process)
        except BaseException:
            self.stop()
            raise

    def stop(self):
        if self.preserve_external_browser:
            try:
                if self.process is not None and self.process.poll() is None:
                    # This service is attachment-only. Kill the driver without
                    # /shutdown, DELETE session, or a graceful SIGTERM handler
                    # that might tell the separately owned Chrome to quit.
                    self.process.kill()
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                if self._owner_job is not None:
                    self._owner_job.close()
                    self._owner_job = None
                if self.process is not None:
                    for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                        if stream is not None:
                            try:
                                stream.close()
                            except OSError:
                                pass
            return
        try:
            super().stop()
        finally:
            if self._owner_job is not None:
                self._owner_job.close()
                self._owner_job = None


class _GuardedExecutor:
    def __init__(self, delegate, service):
        self.delegate, self.service = delegate, service
        self._execute = delegate.execute
        self.disconnected = False
        self.failure = None

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def execute(self, command, params):
        message = ("The local ChromeDriver connection was lost. The bot has stopped; "
                   "no wager command was retried. Inspect any in-flight wager before restarting "
                   "from the bot's owning terminal.")
        if self.disconnected:
            raise self.failure
        started = time.monotonic()
        try:
            return self._execute(command, params)
        except (HTTPError, OSError) as error:
            self.disconnected = True
            self.failure = DriverConnectionLost(message)
            self.failure.command = command
            self.failure.transport_error = type(error).__name__
            self.failure.elapsed_ms = round(1000*(time.monotonic()-started), 1)
            try:
                self.failure.driver_alive = self.service.process.poll() is None
            except (AttributeError, OSError):
                self.failure.driver_alive = None
            raise self.failure from None


def create_owned_chrome(options, executable_path=None, preserve_external_browser=False):
    if preserve_external_browser and not (options.experimental_options.get("debuggerAddress") or options.debugger_address):
        raise ValueError("Preserving Chrome requires attachment to a separately owned browser")
    path = str(executable_path) if executable_path is not None else None
    service = OwnedChromeService(path, preserve_external_browser=True) if preserve_external_browser else OwnedChromeService(path)
    try:
        driver = webdriver.Chrome(service=service, options=options)
    except (HTTPError, OSError):
        service.stop()
        raise DriverConnectionLost("The bot-owned ChromeDriver could not be reached during startup.") from None
    except BaseException:
        service.stop()
        raise
    # Keep Selenium's RemoteConnection instance: BiDi/private-context support
    # checks its concrete type. Guard its dispatch method without replacing it.
    pool = getattr(driver.command_executor, '_conn', None)
    if pool is not None and isinstance(pool.connection_pool_kw, dict):
        pool.connection_pool_kw['retries'] = False
        pool.clear()
    guard = _GuardedExecutor(driver.command_executor, service)
    driver.command_executor.execute = guard.execute
    return driver


def require_owned_driver(driver):
    """Legacy read-only attachments must not be used as a trading runner."""
    if not isinstance(driver, RemoteWebDriver):  # Non-browser test doubles.
        return
    service = getattr(driver, "service", None)
    if not isinstance(service, OwnedChromeService) or service.owner_pid != os.getpid():
        raise RuntimeError("Following requires a ChromeDriver owned by this runner. "
                           "Use start_draftkings.ps1 or resume_draftkings.py --browser-port PORT; "
                           "an inspection-only attachment to an old driver cannot run the bot.")
    if service.process is None or service.process.poll() is not None:
        raise DriverConnectionLost("The bot-owned ChromeDriver has stopped; following was not started.")


def driver_owner_info(driver):
    service = getattr(driver, "service", None)
    if isinstance(service, OwnedChromeService) and service.process is not None:
        return {"runnerPid": service.owner_pid, "driverPid": service.process.pid,
                "driverPort": service.port, "managed": True}
    return None


def close_owned_driver(driver):
    """Release our driver service; an externally opened Chrome stays open."""
    service = getattr(driver, "service", None)
    try:
        driver.command_executor.close()
    finally:
        if isinstance(service, OwnedChromeService) and service.owner_pid == os.getpid():
            service.stop()


def _set_terminal_title(title):
    if os.name == "nt" and sys.stdout.isatty():
        try:
            kernel = ctypes.WinDLL("kernel32")
            kernel.SetConsoleTitleW.argtypes = [wintypes.LPCWSTR]
            kernel.SetConsoleTitleW.restype = wintypes.BOOL
            kernel.SetConsoleTitleW(title)
        except OSError:
            pass


def mark_terminal_stopped():
    _set_terminal_title("SPORTSBOOK BOT - STOPPED")


def mark_terminal_auth_required():
    _set_terminal_title("SPORTSBOOK BOT - SIGN IN REQUIRED")
    if os.name == 'nt' and sys.stdout.isatty():
        try:
            kernel=ctypes.WinDLL('kernel32')
            kernel.GetConsoleWindow.restype=wintypes.HWND
            window=kernel.GetConsoleWindow()
            if window:
                class FlashInfo(ctypes.Structure):
                    _fields_=[('size',wintypes.UINT),('window',wintypes.HWND),('flags',wintypes.DWORD),
                              ('count',wintypes.UINT),('timeout',wintypes.DWORD)]
                info=FlashInfo(ctypes.sizeof(FlashInfo),window,3,5,0)
                ctypes.WinDLL('user32').FlashWindowEx(ctypes.byref(info))
        except OSError:
            pass


def mark_terminal_running():
    _set_terminal_title(BOT_TERMINAL_TITLE)


def announce_driver_owner(driver):
    _set_terminal_title(BOT_TERMINAL_TITLE)
    service = getattr(driver, "service", None)
    if isinstance(service, OwnedChromeService) and service.process is not None:
        print(f"BOT OWNER: runner PID {service.owner_pid}; ChromeDriver PID {service.process.pid}, "
              f"port {service.port}. Keep this terminal open; it owns the driver lifecycle.", flush=True)
