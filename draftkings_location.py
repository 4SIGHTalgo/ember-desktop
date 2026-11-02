"""Read visible DraftKings location notices without changing geolocation or permissions."""

import time

# Shared by the periodic check, slip arming and the final submit command. Browser
# permission is not evidence that GeoComply accepted the user's real location.
LOCATION_FAILURE_JS = r"""
const dkLocationFailure = /(?:can(?:not|'t| not)|could(?: not|n't)|unable to|failed to)\s+(?:verify|confirm|determine|detect|establish)\s+(?:your\s+)?(?:current\s+|physical\s+)?location|(?:having )?trouble (?:verifying|confirming|determining) your (?:current )?location|(?:your\s+)?location\s+(?:(?:could|can) not be|cannot be|was not)\s+(?:verified|confirmed|determined)|location verification (?:has )?failed|outside (?:of )?(?:a |the )?(?:permitted|allowed|authorized) (?:area|location)|does(?:n.t| not) (?:look|seem) like you(?:.re| are)? (?:are )?(?:at|in) (?:a |the )?location where you can (?:place bets|bet)/i;
"""
LOCATION_READER_JS = LOCATION_FAILURE_JS + r"""
const readDkLocation = () => {
    const metadata = {pageUrl: location.origin + location.pathname,
                      documentReady: document.readyState === 'complete'};
    const documents = [document];
    for (let index = 0; index < documents.length && index < 20; index++) {
        for (const frame of documents[index].querySelectorAll('iframe')) {
            try {
                if (frame.getClientRects().length && frame.contentDocument
                    && !documents.includes(frame.contentDocument)) {
                    if(documents.length>=20)return {...metadata,state:'unknown',blocked:true,message:'location document limit exceeded'};
                    documents.push(frame.contentDocument);
                }
            } catch (_) { /* Cross-origin frames cannot be read from the parent. */ }
        }
    }
    const notices = documents.flatMap(doc => [...doc.querySelectorAll(
        '.user-status-banner__content, [role="alert"], [role="dialog"]')]
        .filter(el => el.getClientRects().length && doc.defaultView.getComputedStyle(el).visibility !== 'hidden')
        .map(el => el.innerText || ''));
    const lines = [...notices, ...documents.flatMap(doc => (doc.body?.innerText || '').split(/\n/))]
        .map(line => line.replace(/[\u2018\u2019]/g, "'").replace(/\s+/g, ' ').trim()).filter(Boolean);
    const failure = dkLocationFailure;
    const pending = /(?:verifying|checking|confirming) your (?:current )?location|location (?:check|verification) (?:is )?in progress/i;
    const required = /^(?:please )?verify your (?:current )?location[.!]?$|location verification (?:is )?required|(?:enable|allow|turn on) (?:your )?location (?:access|services)|(?:install|download|enable) (?:the )?(?:geocomply|player location check)/i;
    for (const [pattern, state] of [[failure, 'blocked'], [pending, 'checking'], [required, 'required']]) {
        const message = lines.find(line => pattern.test(line));
        if (message) return {...metadata, state, blocked: true, message: message.slice(0, 400)};
    }
    if (location.hostname === 'sportsbook.draftkings.com' && /^\/auth(?:\/|$)/.test(location.pathname)) {
        return {...metadata, state: 'checking', blocked: true,
                message: 'DraftKings location recheck / authentication is in progress'};
    }
    const verified = lines.find(line => /^(?:your )?location (?:has been |is )?verified[.!]?$|successfully verified your location/i.test(line));
    return {...metadata, state: verified ? 'verified' : 'unknown', blocked: false, message: verified || null};
};
"""

LOCATION_STATE_SCRIPT = LOCATION_READER_JS + "return readDkLocation();"

# Verified on the actual failure banner. Return its normal link for a trusted
# WebDriver click; never infer a retry control from a general "Recheck" label.
LOCATION_RECHECK_SCRIPT = LOCATION_FAILURE_JS + r"""
const origin=new URL(arguments[0]?.origin||'https://sportsbook.draftkings.com').origin;
if(location.origin!==origin)return {status:'unavailable'};
const visible = el => {if(!el?.getClientRects().length)return false;for(let p=el;p;p=p.parentElement){
    const style=getComputedStyle(p);if(style.display==='none'||style.visibility==='hidden'||Number(style.opacity)===0)return false;}return true;};
const matches = [...new Set([...document.querySelectorAll('.user-status-banner__wrapper')].filter(visible)
    .filter(el => dkLocationFailure.test((el.innerText || '').replace(/[\u2018\u2019]/g,"'").replace(/\s+/g, ' ')))
    .flatMap(el => [...el.querySelectorAll('a[href]')]).filter(visible)
    .filter(el => {
        const url = new URL(el.href, location.href);
        const label=el.getAttribute('aria-label')||el.textContent||'';
        return /^(?:recheck|retry|(?:re-?verify|verify) (?:your )?location)$/i.test(label.trim())
            && url.origin === origin
            && !url.username && !url.password && url.pathname.replace(/\/$/,'') === '/auth'
            && (!el.target || el.target === '_self') && el.getAttribute('aria-disabled') !== 'true';
    }))];
return matches.length === 1 ? {status: 'ready', link: matches[0]} : {status: 'unavailable'};
"""


# Seconds after each Recheck before DK's still-visible failure banner may be rechecked again, then
# every RECHECK_REPEAT. Live 2026-10-28 one hung click, never retried, left wagering paused for 5 h.
RECHECK_BACKOFF = (120, 240, 480, 960)
RECHECK_REPEAT = 1800
# After this long a recheck's page counts as settled even if it never reports readyState
# 'complete'; native sign-in, can-bet and the wagering grant still gate resumption.
RECHECK_SETTLE = 60


class LocationRecheck:
    """DK's official Recheck link, retried with backoff while its failure stays; never resubmit a wager."""

    def __init__(self, state=None, *, clock=None):
        self.report = dict(state or {"attempted": False})
        self.hold_until = 0
        self.clock = clock if clock is not None else lambda: time.monotonic()
        self.attempts = 1 if self.report.get("attempted") else 0
        # A restored reservation is never replayed at once: it waits out the first backoff.
        now = self.clock()
        self.retry_at = now + RECHECK_BACKOFF[0] if self.attempts else 0
        self.settle_until = now + RECHECK_SETTLE if self.attempts else 0

    def start(self, driver, reading, validate=None):
        if (reading.get("state") != "blocked" or self.report.get("attempted") and self.clock() < self.retry_at
                or validate is not None and not validate()):
            return False
        result = driver.execute_script(LOCATION_RECHECK_SCRIPT)
        if not isinstance(result, dict) or result.get("status") != "ready" or result.get("link") is None:
            return False
        # Reserve the attempt before clicking: a lost WebDriver reply must not replay
        # the /auth action before its backoff. No login credentials or wager is submitted.
        now = self.clock()
        self.attempts += 1
        self.report = {"attempted": True, "status": "attempted", "sourceUrl": reading.get("pageUrl"), "attempts": self.attempts}
        self.hold_until = now + 2
        self.settle_until = now + RECHECK_SETTLE
        self.retry_at = now + (RECHECK_BACKOFF[self.attempts-1] if self.attempts <= len(RECHECK_BACKOFF) else RECHECK_REPEAT)
        if validate is not None and not validate():
            self.report['status'] = 'cancelled'
            return False
        result["link"].click()
        self.report["status"] = "started"
        print("Location recheck: opened DraftKings' official Recheck link; waiting for verification.", flush=True)
        return True

    def permission_observed(self):
        self.report = {'attempted': False, 'status': 'permission_restored'}
        self.hold_until = self.retry_at = self.settle_until = 0
        self.attempts = 0


def update_location(driver, bettor, recheck=None, stage="periodic"):
    """Refresh after navigation as well as on the timer; keep auth/loading paused."""
    reading = location_status(driver)
    if (not reading.get("blocked") and "location" in bettor.pause_reasons
            and (reading.get("documentReady") is False
                 or recheck is not None and time.monotonic() < recheck.hold_until)):
        reading = {**reading, "state": "checking", "blocked": True,
                   "message": "Waiting for the DraftKings location recheck page to settle"}
    reading["checkedAfter"] = stage
    bettor.location = reading
    message = location_change(bettor, reading)
    if message:
        print(message, flush=True)
    if recheck is not None and bettor.stake is not None and bettor.stopped is None:
        recheck.start(driver, reading)
    return reading


def location_status(driver):
    result = driver.execute_script(LOCATION_STATE_SCRIPT)
    if not isinstance(result, dict) or result.get("state") not in (
            "blocked", "checking", "required", "verified", "unknown"):
        return {"state": "unknown", "blocked": False, "message": None}
    return result


def location_change(bettor, result):
    """A location pause is independent of a login pause; no success is inferred."""
    previous = bettor.pause_reasons.get("location")
    if result.get("blocked"):
        reason = "DraftKings location verification: " + (result.get("message") or result["state"])
        bettor.pause(reason, source="location")
        if reason != previous:
            return (f"Betting paused: {reason}. Complete the real location check in this same browser. "
                    "A successful login does not verify location.")
    elif previous:
        bettor.resume(source="location")
        return ("DraftKings location warning is no longer visible; "
                + ("betting checks resumed. " if bettor.status == "active" else "betting remains paused. ")
                + ("Location verified by the visible notice." if result["state"] == "verified" else
                   "Location acceptance is still unconfirmed; absence of a warning is not verification."))
    return None
