"""Bounded, non-wager navigation between the league and a verified event."""
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from ember_protocol import timestamp
from tt_market_semantics import entry_window


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

    def focus_event_id(self):
        """The event whose page the browser is being kept on, if any."""
        return self.key[0] if self.key else None

    def focus_target(self, hint, stages, legs, server_now):
        """One monitoring focus from the stage catalog, not just the server hint.

        An open entry window with an unstaged leg outranks everything; after
        that, matches still missing slip legs are pre-staged nearest-break
        first, and fully staged matches only need warming.
        """
        candidates = {}
        for stage in stages:
            event = stage.get("event") if isinstance(stage, dict) else None
            if not isinstance(event, dict) or timestamp(stage["expires_at"]) <= server_now:
                continue
            identifier = str(event.get("eventId"))
            if identifier and identifier not in candidates:
                candidates[identifier] = (event, stage.get("bet_type"))
        if not candidates:
            if hint and server_now < timestamp(hint["expires_at"]):
                return hint["event"], hint["market_hint"]["betType"]
            return None, None
        present = {(leg.get("eventId"), leg.get("marketId"), leg.get("selectionId"))
                   for leg in legs or [] if isinstance(leg, dict)}
        stages = stages if isinstance(stages, list) else []

        def entry_open(event):
            try:
                return bool(entry_window(event))
            except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
                return False

        def missing(identifier):
            return any(str(stage.get("event_id")) == identifier and
                       (stage.get("event_id"), stage.get("market_id"), stage.get("selection_id")) not in present
                       for stage in stages)

        def points(event):
            current = event.get("currentSet") or {}
            return max((value for value in (current.get("home"), current.get("away"))
                        if type(value) is int), default=0)

        hint_id = str((hint or {}).get("event", {}).get("eventId") or "")
        def rank(identifier):
            event, _ = candidates[identifier]
            urgency = 0 if entry_open(event) and missing(identifier) else 1 if missing(identifier) else 2
            return (urgency, -points(event), identifier != hint_id, identifier)
        best = min(candidates, key=rank)
        current = self.key[0] if self.key else None
        if current in candidates and rank(current)[0] == rank(best)[0]:
            # Sticky focus; only a clearly closer unstaged match takes over.
            if not (rank(best)[0] == 1 and rank(best)[1] < rank(current)[1]-1):
                best = current
        family = candidates[best][1]
        if hint and str(hint["event"].get("eventId")) == best:
            family = hint["market_hint"]["betType"]
        return candidates[best][0], family

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

    def step(self, driver, hint, feed_ready, server_now, hold=False, stages=None, legs=None):
        now = self.clock()
        if not feed_ready:
            self.publish("waiting_feed", "Live source connection is paused; browser preparation is waiting for fresh data.", self.key[0] if self.key else None)
            return
        if self.market_readiness is not None and not self.market_readiness.provider_ready(driver):
            self.publish('provider_loading', self.market_readiness.error_reason)
            return
        event = family = None
        if stages:
            event, family = self.focus_target(hint, stages, legs, server_now)
        elif hint and server_now < timestamp(hint["expires_at"]):
            event, family = hint["event"], hint["market_hint"]["betType"]
        if event is None:
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
        key = (str(event["eventId"]), family)
        if key != self.pending_key:
            self.pending_key, self.next_attempt, self.failures = key, 0, 0
        if now < self.next_attempt:
            return
        self.next_attempt = now+2
        try:
            if self.key and self.key[0] == key[0] and self.core.correct_event(driver, event):
                self.key = key
                self.monitor(driver, event, {"betType": family})
                return
            self.publish("preparing_event", f"Preparing {event['player1']} vs {event['player2']} ({family}).", event["eventId"])
            # Every event navigation needs the same auth-hydration grace as the
            # initial league navigation, including retries on a loading page.
            self.navigation_started()
            opened = self.core.open_props(driver, event, {"betType": family}, timeout=2.5)
        except self.core.FATAL_BROWSER_ERRORS:
            raise
        except (RuntimeError, self.core.WebDriverException):
            opened = False
        if opened:
            self.key, self.failures = key, 0
            self.monitor(driver, event, {"betType": family})
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
