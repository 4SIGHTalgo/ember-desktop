"""Never send a page command into a navigation that has not committed (execution layer).

Chrome holds every DevTools command for a navigating tab until the new document commits:
a 6 s slow document held ChromeDriver and a raw DevTools heartbeat for the full 6 s (fixture,
2026-10-27), and a held command that outlives the driver timeout poisons the session and forces a
full recovery. Page.getNavigationHistory is answered by the browser process during that hold
(fixture: 1-15 ms) and its current entry changes exactly at commit, so the gate can refuse a page
command without sending it. A refused command raises NavigationPending: it provably never reached
the page, and callers treat it as "this tab is not ready yet".
"""
import time

from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.remote.errorhandler import ErrorHandler


class NavigationPending(WebDriverException):
    """The tab is still waiting for its new document; the command was not sent."""


# Commands ChromeDriver answers from the browser process or its own session state.
BROWSER_SIDE = frozenset({"get", "newWindow", "switchToWindow", "close", "quit", "w3cGetWindowHandles",
                          "w3cGetCurrentWindowHandle", "getLog", "getAvailableLogTypes", "getTimeouts", "setTimeouts"})
BROWSER_CDP = ("Page.getNavigationHistory", "Page.bringToFront", "Target.", "Browser.")


class NavigationGate:
    WAIT = .5  # a commit this close is waited for; a longer load yields the turn instead of blocking it
    ABANDON = 60  # shortcut: a navigation that never commits is left to the driver timeout and normal recovery

    def __init__(self, guard, clock=time.monotonic, sleep=time.sleep, settled=None):
        self.guard, self.clock, self.sleep = guard, clock, sleep
        self.settled = settled  # settled(load_ms, refused_commands): local evidence after each tracked load
        self.refused = 0
        self.current = None  # the session's window handle; None means ask the browser
        self.pending = {}  # handle -> (committed entry id before the navigation, started)
        self.starting = self.closing = self.session = None

    def query(self, command, params, valid):
        # The raw executor returns driver errors as data (a 4xx body string); raise them as the driver would.
        response = self.guard.execute(command, {"sessionId": self.session, **params})
        if not isinstance(response, dict):
            raise WebDriverException("The driver returned an unreadable response.")
        try:
            ErrorHandler().check_response(response)
        except TimeoutException:
            # Live 2026-10-28: a busy renderer timed out this browser-side query twice and each became a
            # full recovery; the page command is still unsent, so this tab is just not ready yet.
            raise NavigationPending("The tab did not answer its navigation history; the command was not sent.") from None
        if not valid(response.get("value")):
            raise WebDriverException("The driver returned an unreadable response.")
        return response["value"]

    def entry(self):
        value = self.query("executeCdpCommand", {"cmd": "Page.getNavigationHistory", "params": {}}, lambda v:
            isinstance(v, dict) and isinstance(v.get("entries"), list) and type(v.get("currentIndex")) is int
            and 0 <= v["currentIndex"] < len(v["entries"]) and isinstance(v["entries"][v["currentIndex"]], dict))
        return value["entries"][value["currentIndex"]]

    def handle(self):
        if self.current is None:
            self.current = self.query("w3cGetCurrentWindowHandle", {}, lambda v: isinstance(v, str) and v)
        return self.current

    def before(self, command, params):
        self.session = (params or {}).get("sessionId", self.session)  # Selenium adds it to each session command.
        if command == "get":
            entry = self.entry()
            # A same-URL load (a reload) keeps its entry id, so its commit is not observable here.
            self.starting = None if entry.get("url") == (params or {}).get("url") else (self.handle(), entry.get("id"))
            return
        if command in ("switchToWindow", "newWindow", "close"):
            self.closing = self.current if command == "close" else None
            self.current = None
            return
        if command in BROWSER_SIDE or (command == "executeCdpCommand" and str((params or {}).get("cmd", "")).startswith(BROWSER_CDP)):
            return
        if not self.pending or self.handle() not in self.pending:
            return
        before, started = self.pending[self.current]
        deadline = self.clock()+self.WAIT
        while True:
            if self.entry().get("id") != before or self.clock()-started > self.ABANDON:
                self.pending.pop(self.current, None)
                if self.settled is not None:
                    self.settled(1000*(self.clock()-started), self.refused)
                self.refused = 0
                return
            if self.clock() >= deadline:
                self.refused += 1
                raise NavigationPending("The tab is still loading a new page; the command was not sent.")
            self.sleep(.05)

    def after(self, command, params, response):
        if command == "get" and self.starting:
            handle, before = self.starting
            self.pending[handle] = (before, self.clock())
        elif command == "switchToWindow":
            self.current = (params or {}).get("handle")
        elif command == "newWindow":
            self.current = ((response or {}).get("value") or {}).get("handle")
        elif command == "close":
            self.pending.pop(self.closing, None)
        self.starting = None
