"""Bounded, non-wager navigation between the league and a verified event."""
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from ember_protocol import timestamp


class BrowserPreparation:
    def __init__(self, core, navigation_started, open_league, notify, clock=time.monotonic, market_readiness=None):
        self.core, self.navigation_started, self.open_league = core, navigation_started, open_league
        self.notify, self.clock = notify, clock
        self.key = self.pending_key = None
        self.next_attempt = 0
        self.no_hint_since = None
        self.failures = 0
        self.last_status = None
        self.last_publish = float("-inf")
        self.market_readiness = market_readiness

    def monitor(self, driver, event, market):
        if self.market_readiness is not None:
            try:
                reading = self.market_readiness.warm(driver, event, market)
            except self.core.FATAL_BROWSER_ERRORS:
                raise
            except (RuntimeError, self.core.WebDriverException):
                reading = {"status": "waiting", "reason": "market surface not ready"}
            if reading.get("status") != "ready":
                self.publish("preparing_market", f"Match open; preparing the exact market section ({reading.get('reason') or 'loading'}).", event["eventId"])
                return
        self.publish("watching_event", f"Watching verified match {event['player1']} vs {event['player2']}; awaiting a qualifying model signal.", event["eventId"])

    def publish(self, state, text, event_id=None):
        value = (state, text, event_id)
        changed = value != self.last_status
        if changed or self.clock()-self.last_publish >= 5:
            self.last_status = value
            self.last_publish = self.clock()
            self.notify(state=state, text=text, event_id=event_id, changed=changed,
                        checked_at=datetime.now(timezone.utc).isoformat())

    def at_league(self, driver):
        address = urlsplit(driver.current_url)
        expected = urlsplit(self.core.LEAGUE_URL)
        return address.scheme == expected.scheme and address.netloc == expected.netloc and address.path.rstrip("/") == expected.path.rstrip("/")

    def league(self, driver):
        self.next_attempt = self.clock()+3
        self.navigation_started()
        self.open_league(driver)
        self.key = None

    def step(self, driver, hint, feed_ready, server_now, hold=False):
        now = self.clock()
        if not feed_ready:
            self.publish("waiting_feed", "Live source connection is paused; browser preparation is waiting for fresh data.", self.key[0] if self.key else None)
            return
        if not hint or server_now >= timestamp(hint["expires_at"]):
            if hold:
                self.no_hint_since = None
                self.publish("waiting_candidate", "Retaining staged live matches while waiting for fresh signals.", self.key[0] if self.key else None)
                return
            self.no_hint_since = now if self.no_hint_since is None else self.no_hint_since
            if now >= self.next_attempt and ((self.key and now-self.no_hint_since >= 3) or (self.key is None and not self.at_league(driver))):
                try:
                    self.league(driver)
                except self.core.FATAL_BROWSER_ERRORS:
                    raise
                except (RuntimeError, self.core.WebDriverException):
                    self.next_attempt = self.clock()+10
                    self.publish("league_loading", "Waiting for the TT Elite league match list to load.")
                    return
            if self.key:
                self.publish("waiting_candidate", "Waiting briefly for a fresh verified monitoring target.", self.key[0])
            elif self.at_league(driver):
                self.publish("league_standby", "TT Elite league standby: waiting for a verified live match to prepare.")
            else:
                self.publish("league_loading", "Waiting for the TT Elite league match list to load.")
            return
        self.no_hint_since = None
        event, market = hint["event"], hint["market_hint"]
        key = (event["eventId"], market["betType"])
        if key != self.pending_key:
            self.pending_key, self.next_attempt, self.failures = key, 0, 0
        if now < self.next_attempt:
            return
        self.next_attempt = now+2
        try:
            if self.key == key and self.core.correct_event(driver, event):
                self.monitor(driver, event, market)
                return
            self.publish("preparing_event", f"Preparing {event['player1']} vs {event['player2']} ({market['betType']}).", event["eventId"])
            # Every event navigation needs the same auth-hydration grace as the
            # initial league navigation, including retries on a loading page.
            self.navigation_started()
            opened = self.core.open_props(driver, event, market, timeout=.5)
        except self.core.FATAL_BROWSER_ERRORS:
            raise
        except (RuntimeError, self.core.WebDriverException):
            opened = False
        if opened:
            self.key, self.failures = key, 0
            self.monitor(driver, event, market)
            return
        self.failures += 1
        if self.failures >= 5 and not hold:
            try:
                self.league(driver)
            except self.core.FATAL_BROWSER_ERRORS:
                raise
            except (RuntimeError, self.core.WebDriverException):
                pass
            self.next_attempt = self.clock()+10
            self.failures = 0
            self.publish("event_retry", "Candidate page did not become ready; returning to league standby before a bounded retry.", event["eventId"])
