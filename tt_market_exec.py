"""Shared market execution: browser navigation, slip plumbing, and fail-closed
in-page guards that drive DraftKings on the customer's machine.

The executor owns no strategy. It navigates to the verified event, reveals the
exact market, clicks the quoted selection, arms and confirms the slip, and
aborts on any ambiguity, price move, or expiry. Decisions (EV, sizing) arrive
as validated parameters; the scanner and CLI strategy live in tt_elite_follow.

Layer: execution (Selenium plus the shared slip protocol). No strategy imports.
"""
from datetime import datetime, timezone
import json
import math
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from selenium.common.exceptions import (ElementClickInterceptedException, InvalidSessionIdException, NoSuchWindowException,
                                        StaleElementReferenceException, TimeoutException, WebDriverException)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait

from driver_lifecycle import (DriverConnectionLost, mark_terminal_stopped,
                              mark_terminal_auth_required, mark_terminal_running)
from draftkings_location import (LOCATION_READER_JS, LocationRecheck, location_change,
                                 location_status, update_location)
from wager_sizing import BALANCE_READER_JS
from wager_submission import SUBMISSION_UI_JS, LOAD_RECOVERY_JS, recover_load_notice, settle_submission
from execution_timing import ExecutionTiming
from tt_market_semantics import closeout_state, name_key, score_state, american_odds
from execution_policy import MARKET_FAMILIES
from ember_slip import EXACT_SLIP_READER_JS, plan_slip_config

ROOT = Path(__file__).resolve().parent

LEAGUE_URL = "https://sportsbook.draftkings.com/leagues/tabletennis/tt-elite-series"
DISPATCH_TARGET_MS = 1000
SLIP_ARM_TIMEOUT = 10
PLAN_READY_TIMEOUT = .25
SELECTION_CLICK_TIMEOUT = 2.0
SELECTION_DELIVERY_TIMEOUT = 2.0
SLIP_ARM_INTERVAL = .1  # Browser-side fallback; DOM mutations wake readiness immediately.
SLIP_CAPTURE = ROOT / "dk_bet_slip_poc.json"
# What to do when the price moves between selecting the bet and submitting it.
# accept: take the new price; better: take it only if it improved; none: abort.
SLIPPAGE_CHOICES = ("accept", "better", "none")
DEFAULT_SLIPPAGE = "accept"
# Legacy interval setting for session maintenance; positive values are capped at
# 45 seconds to feed the site's one-minute activity-driven heartbeat. 0 disables
# maintenance. The native server still enforces account/session restrictions.
DEFAULT_KEEP_ALIVE = 300
# A browser that answers nothing for this long is gone; stop with a clear report
# instead of spinning forever. Individual WebDriver hiccups are survivable.
BROWSER_SILENCE_LIMIT = 180
# Losing the window or the session is the only browser state nothing can recover
# from in place, so it still ends the run.
FATAL_BROWSER_ERRORS = (InvalidSessionIdException, NoSuchWindowException, DriverConnectionLost)
VISIBLE_UI_JS = """
const visibleUI = element => {
    if (!element || !element.getClientRects().length) return false;
    for (let node = element; node; node = node.parentElement) {
        const style = getComputedStyle(node);
        if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse'
            || Number(style.opacity) === 0 || style.contentVisibility === 'hidden') return false;
    }
    return true;
};
"""
REVEAL_SCRIPT = VISIBLE_UI_JS + SUBMISSION_UI_JS + (ROOT / "reveal_next_set.js").read_text(encoding="utf-8")


def correct_event(browser, event):
    url = urlparse(browser.current_url)
    try:
        return (url.hostname == "sportsbook.draftkings.com"
                and url.path.rstrip("/").split("/")[-1] == event["eventId"]
                and any(panel.is_displayed() and all(f" {name_key(event[player])} " in f" {name_key(panel.text)} "
                                                    for player in ("player1", "player2"))
                        for panel in browser.find_elements(By.CSS_SELECTOR, ".cms-event-scoreboard-container")))
    except StaleElementReferenceException:
        return False


class BrowserThrottled(RuntimeError):
    """The site asks the shared browser lane to back off."""


class BrowserNavigationPending(RuntimeError):
    """The event is valid, but its UI is still rendering or a tab is obstructed."""


def event_route_open(driver, event):
    address = driver.current_url
    if not isinstance(address, str):
        return False
    page = urlparse(address)
    return page.hostname == 'sportsbook.draftkings.com' and page.path.rstrip('/').split('/')[-1] == event['eventId']


def surface_family(bet_type):
    """The prepared surface a market family lives on."""
    if bet_type in ("set_handicap", "point_handicap"):
        return "handicap"
    # The complement rides the same Winner - Listed Game surface as the split.
    if bet_type == "complement":
        return "split"
    return bet_type


def market_surface(market=None):
    """Map feed market names to the provider's displayed section headers."""
    MARKETS = MARKET_FAMILIES
    market = market or {}
    family = market.get('betType', 'split')
    if family == 'handicap':
        # The prepared spread surface covers both handicap markets at once.
        family = 'set_handicap'
    name = market.get('name')
    if name not in MARKETS:
        name = next((name for name, value in MARKETS.items() if value == family), name)
    distribution = name in MARKETS
    if distribution:
        titles = {'Games Spread': ['Spread', 'Games Spread'],
                  'Points Spread': ['Spread', 'Points Spread'],
                  'Correct Score': ['Correct Score BO5', 'Correct Score'],
                  'Total Points': ['Total', 'Total Points']}[name]
    else:
        titles = ['Moneyline'] if family == 'closeout' or name == 'Moneyline' else ['Winner - Listed Game']
    match_market = distribution or titles == ['Moneyline']
    return {'sectionTitle': titles[0], 'sectionTitles': titles,
            'rowNames': list(dict.fromkeys(([name] if name else []) + titles + (['Match Winner'] if titles == ['Moneyline'] else []))),
            'tabLabels': ['Match Lines', 'Popular'] if match_market else ['Player Props', 'Game Props', 'Match Lines', 'Popular'],
            'distribution': distribution}


def market_tab(driver, market=None):
    """Keep a suitable already-selected view; exact market IDs remain mandatory."""
    surface = market_surface(market)
    section = surface['sectionTitles']
    labels = (surface['tabLabels'] if surface['distribution'] else ['Match Lines']
              if surface['sectionTitle'] == 'Moneyline' else ['Player Props', 'Game Props', 'Match Lines'])
    reading = driver.execute_script(MARKET_TAB_STATE_SCRIPT,
                                    {'section': surface['sectionTitle'], 'sections': section,
                                     'labels': labels, 'matchMarket': surface['distribution']})
    if isinstance(reading,dict) and 'tab' in reading:
        return reading['tab']
    for tab in driver.find_elements(By.XPATH, "//*[@role='tab' and normalize-space(.)='Popular']"):
        if (tab.is_displayed() and tab.text.strip() == 'Popular' and tab.get_attribute('aria-selected') == 'true'
                and driver.execute_script(MARKET_SECTION_SCRIPT, section) is True):
            return tab
    for label in labels:
        for tab in driver.find_elements(By.XPATH, f"//*[@role='tab' and normalize-space(.)='{label}']"):
            if tab.is_displayed() and tab.text.strip() == label:
                return tab
    return None


def native_navigation_click(driver, element, label=None):
    reading = driver.execute_script(NAVIGATION_CLICK_SCRIPT, element, label)
    if not isinstance(reading, dict) or reading.get('status') != 'ready':
        driver._tt_navigation_readiness = reading if isinstance(reading, dict) else {'reason': 'navigation target unavailable'}
        return False
    if reading.get('alternatePoint'):
        point = reading['clickPoint']
        ActionChains(driver).move_to_element_with_offset(element, point['offsetX'], point['offsetY']).click().perform()
    else:
        element.click()
    driver._tt_navigation_readiness = {'reason':'waiting for navigation control to settle'}
    return True


def ensure_market_tab(driver, event, market=None, timeout=.4, verified=False):
    deadline = time.monotonic() + timeout
    clicked = False
    first = True
    while True:
        if not (first and verified) and not correct_event(driver, event):
            return False
        first = False
        try:
            tab = market_tab(driver, market)
            if tab is not None:
                if tab.get_attribute('aria-selected') == 'true':
                    driver._tt_navigation_readiness = None
                    return True
                if not clicked:
                    clicked = native_navigation_click(driver, tab, tab.text.strip())
        except (ElementClickInterceptedException, StaleElementReferenceException):
            clicked = False
        if time.monotonic() >= deadline:
            return False
        time.sleep(.02)


def prepare_browser_execution(driver):
    # Chrome marks a covered/background window hidden and pauses animation
    # frames. DK's entrance transition can then remain at opacity:0 forever,
    # making real Selenium visibility/name/tab checks fail on a rendered page.
    # Keep that page's normal foreground scheduling; never change site CSS.
    driver.set_page_load_timeout(15)
    driver.execute_cdp_cmd("Emulation.setFocusEmulationEnabled", {"enabled": True})


def open_props(driver, event, market=None, timeout=15):
    deadline = time.monotonic() + max(0, timeout)
    slug = "-vs-".join(name_key(event[player]).replace(" ", "-") for player in ("player1", "player2"))
    navigation = "already open"
    if not correct_event(driver, event) and not event_route_open(driver, event):
        pending = getattr(driver, '_tt_navigation_intent', None)
        if isinstance(pending, dict) and time.monotonic() < pending['until']:
            return False
        # Prefer the site's native routing so an event change need not tear down
        # the running GeoComply client. Reuse the current page when already there.
        identifier = event["eventId"]
        expired_intent = isinstance(pending, dict) and pending['event'] == event['eventId']
        links = [] if expired_intent else driver.find_elements(By.CSS_SELECTOR,
                                     f'a[href*="/event/"][href$="/{identifier}"], a[href*="/event/"][href$="/{identifier}/"]')
        # Player-name anchors can have their own click handlers. The actual
        # event's More Bets action is the reliable router entry point.
        links = sorted(links, key=lambda link: str(link.text).strip().casefold() != "more bets")
        for link in links:
            target = urlparse(link.get_attribute("href") or "")
            if (link.is_displayed() and target.scheme == "https"
                    and target.netloc == "sportsbook.draftkings.com"
                    and target.path.startswith("/event/")
                    and target.path.rstrip("/").split("/")[-1] == event["eventId"]
                    and link.get_attribute("target") in (None, "", "_self")):
                try:
                    driver._tt_navigation_intent = {'event': identifier, 'until': time.monotonic()+5}
                    if not native_navigation_click(driver, link):
                        driver._tt_navigation_intent = None
                        continue
                    WebDriverWait(driver, max(.001, deadline-time.monotonic()), poll_frequency=.05).until(
                        lambda d: urlparse(d.current_url).path.rstrip("/").split("/")[-1] == event["eventId"])
                except (ElementClickInterceptedException, StaleElementReferenceException):
                    driver._tt_navigation_intent = None
                    continue
                except TimeoutException:
                    return False
                navigation = "in-page event link"
                break
        else:
            try:
                driver._tt_navigation_intent = {'event': event['eventId'], 'until': time.monotonic()+5}
                driver.get(f"https://sportsbook.draftkings.com/event/{slug}/{event['eventId']}")
            except TimeoutException:
                # Analytics/stream requests can keep the load open after the
                # usable event has rendered. Accept only its verified identity.
                if not correct_event(driver, event):
                    raise
            navigation = "direct load"

    try:
        def wait(predicate):
            return WebDriverWait(driver, max(.001, deadline - time.monotonic()), poll_frequency=.05).until(predicate)
        wait(lambda d: correct_event(d, event))
        driver._tt_navigation_intent = None
        tab = wait(lambda d: market_tab(d, market))
        label = tab.text.strip()
        if tab.get_attribute('aria-selected') != 'true' and not ensure_market_tab(
                driver, event, market, min(.4, max(.05, deadline - time.monotonic())), verified=True):
            raise BrowserNavigationPending('verified event is waiting for an unobstructed market tab')
    except TimeoutException:
        bodies = driver.find_elements(By.TAG_NAME, "body")
        text = bodies[0].text if bodies else ""
        if re.search(r"too many requests|rate limit|service unavailable|HTTP\s*(?:429|503)", text, re.I):
            raise BrowserThrottled("DraftKings is rate-limiting or temporarily unavailable; backing off.") from None
        if "Access Denied" in text or "Network Error" in text:
            raise RuntimeError("DraftKings blocked or failed to load the event page. Inspect this browser before restarting.") from None
        if urlparse(driver.current_url).hostname == "myaccount.draftkings.com":
            raise RuntimeError("DraftKings needs sign-in again. Complete login in the browser before restarting.") from None
        return False
    driver._tt_navigation_readiness = None
    print(f"Opened {label}: {event['player1']} vs {event['player2']} ({event['eventId']}); "
          f"navigation: {navigation}.", flush=True)
    return True


def reveal_set_winner(driver, event, market, display=None, *, staging=False):
    if not correct_event(driver, event):
        driver.execute_script(REVEAL_SCRIPT, None)
        return {"status": "wrong event", "missingRow": False}
    moneyline = market.get("name") == "Moneyline"
    MARKETS = MARKET_FAMILIES
    distribution = market.get("name") in MARKETS
    if moneyline and staging is not True and (market["name"] != "Moneyline" or closeout_state(event) is None
                      or market.get("scoreState") != score_state(event)):
        return {"status": "wrong event", "missingRow": False,
                "reason": "Moneyline is not a verified close-out state"}
    if not ensure_market_tab(driver, event, market, timeout=.15, verified=True):
        reading = getattr(driver, '_tt_navigation_readiness', None)
        reason = reading.get('reason') if isinstance(reading, dict) else None
        return {'status':'waiting','missingRow':False,'reason':reason or 'waiting for the exact market tab'}
    outcomes = market["outcomes"]
    config = {**market_surface(market), "key": f"{event['eventId']}:{market['marketId']}", "name": market["name"],
              "pageUrl": driver.current_url.split("?")[0],
              "players": [event["player1"], event["player2"]],
              "labels": [o["label"] for o in outcomes],
              "testIds": [f"component-builder-market-button-{event['eventId']}-{o['selectionId']}" for o in outcomes],
              "validUntil": min(datetime.fromisoformat(r["receivedAt"]).timestamp() for r in (event, market)) * 1000 + 90000}
    if moneyline:
        config.update(betType="closeout")
    elif distribution:
        config.update(betType=MARKETS[market['name']])
    if display is not None:
        config.update(**display)
    return driver.execute_script(REVEAL_SCRIPT, config)


def browser_finished(driver, event_id):
    url = urlparse(driver.current_url)
    if url.hostname != "sportsbook.draftkings.com" or url.path.rstrip("/").split("/")[-1] != event_id:
        return False
    for panel in driver.find_elements(By.CSS_SELECTOR, ".cms-event-scoreboard-container"):
        if panel.is_displayed() and (re.search(r"\bFinal\b", panel.text)
                                     or "It looks like this event has ended." in panel.text):
            return True
    return False


def dead_event_page(driver, event_id):
    """True when the browser is sitting on that event's own '404 Not Found' page."""
    try:
        page = urlparse(driver.current_url)
        return (page.hostname == "sportsbook.draftkings.com"
                and page.path.rstrip("/").split("/")[-1] == event_id
                and (driver.title or "").startswith("404"))
    except WebDriverException:
        return False


class BrowserTroubles:
    """Watches the browser's pulse: a dead session fails now, a quiet one soon.

    Every guarded driver call reports here. Individual failures are printed and
    swallowed so the loop keeps following; a session whose window is gone raises
    immediately, and one that has answered nothing for a long stretch becomes a
    clear stop instead of an endless silent spin.
    """

    def __init__(self, limit=BROWSER_SILENCE_LIMIT):
        self.limit = limit
        self.last_ok = time.time()
        self.last_notice = 0.0

    def ok(self):
        self.last_ok = time.time()

    def fail(self, where, error):
        if isinstance(error, FATAL_BROWSER_ERRORS):
            raise error
        silent = time.time() - self.last_ok
        if silent >= self.limit:
            raise RuntimeError(
                f"The browser stopped answering ({int(silent)}s without a reply; "
                f"last problem during {where}: {type(error).__name__}).") from error
        now = time.time()
        if now - self.last_notice >= 30:
            print(f"Browser problem during {where} ({type(error).__name__}); continuing.", flush=True)
            self.last_notice = now


CLICK_SCRIPT = VISIBLE_UI_JS + SUBMISSION_UI_JS + """
if(loadNotice())return {status:'blocked',reason:'provider load error; selection clicks paused'};
const cfg = arguments[0];
if (cfg.validUntil && Date.now() > cfg.validUntil) {
    return {status: 'aborted', reason: 'selection quote expired before the click'};
}
const normalize = text => (text || '').toLowerCase().replace(/[łøđ]/g, ch => ({ł: 'l', ø: 'o', đ: 'd'})[ch])
    .normalize('NFKD').replace(/\\p{M}/gu, '').replace(/[^\\p{L}\\p{N}]+/gu, ' ').trim();
const visibleChoice = element => {
    if (!element.getClientRects().length) return false;
    for (let node=element; node; node=node.parentElement) {
        const style=getComputedStyle(node);
        if (style.display==='none' || style.visibility==='hidden' || Number(style.opacity)===0) return false;
    }
    return true;
};
const buttons = [...document.querySelectorAll('button[data-testid]')]
    .filter(b => b.getAttribute('data-testid') === cfg.testId && visibleChoice(b));
if (buttons.length !== 1) return {status: 'aborted', reason: 'selection button missing or ambiguous'};
const button = buttons[0];
if (button.disabled || button.getAttribute('aria-disabled') === 'true' || !button.getClientRects().length) {
    return {status: 'aborted', reason: 'selection is disabled or hidden'};
}
if (normalize(button.querySelector('[data-testid="button-title-market-board"]')?.textContent || '')
        !== normalize(cfg.label)) {
    return {status: 'aborted', reason: 'selection name changed'};
}
if (!/^[+\\-\\u2212]?\\d+$/.test((button.querySelector('[data-testid="button-odds-market-board"]')?.textContent || '').trim())) {
    return {status: 'aborted', reason: 'selection price is unreadable'};
}
// The board can re-render before its selected styling catches up. The exact
// contract already present in the slip must never receive a second toggle.
const inSlip = cfg.selectionId && [...document.querySelectorAll(
    '[data-testid="betslip-content-wrapper"] [data-testid="betslip-selection-card-ex-button"]')]
    .some(remove => {
        try { return JSON.parse(remove.getAttribute('data-tracking') || '{}').selectionId === cfg.selectionId; }
        catch (_) { return false; }
    });
if (button.getAttribute('aria-pressed') === 'true' || inSlip) {
    return {status: 'selected', reason: inSlip ? 'exact selection already in the slip' : 'odds button already selected'};
}
button.scrollIntoView({block:'center',inline:'center',behavior:'instant'});
const rect=button.getClientRects()[0];
const left=Math.max(0,rect.left),right=Math.min(innerWidth,rect.right),
      top=Math.max(0,rect.top),bottom=Math.min(innerHeight,rect.bottom);
const center={x:Math.floor((left+right)/2),y:Math.floor((top+bottom)/2)};
const describe=el=>el?{tag:el.tagName,testId:el.getAttribute('data-testid'),
    classes:typeof el.className==='string'?el.className.slice(0,250):null}:null;
const centerHit=left<right&&top<bottom?document.elementFromPoint(center.x,center.y):null;
const points=[center];
for (const y of [.2,.8]) for (const x of [.2,.5,.8]) {
    points.push({x:Math.floor(left+(right-left)*x),y:Math.floor(top+(bottom-top)*y)});
}
const point=left<right&&top<bottom?points.find(p=>button.contains(document.elementFromPoint(p.x,p.y))):null;
const geometry={center,blocker:button.contains(centerHit)?null:describe(centerHit),
    pointerEvents:getComputedStyle(button).pointerEvents};
if (!point) return {status:'blocked',reason:'selection click target is obstructed or outside the viewport',geometry};
const american=(button.querySelector('[data-testid="button-odds-market-board"]')?.textContent||'').trim();
if (cfg.inspectOnly) return {status:'ready',geometry,american};
// Observe delivery, without intercepting or synthesizing the site's events.
window.__ttSelectionDelivery?.controller.abort();
const controller=new AbortController();
const delivery={testId:cfg.testId,nativeClicks:0,pointerDowns:0,controller};
window.__ttSelectionDelivery=delivery;
for (const type of ['pointerdown','click']) document.addEventListener(type,event=>{
    if (event.isTrusted&&event.composedPath().some(el=>el?.getAttribute?.('data-testid')===cfg.testId)) {
        if(type==='click')delivery.nativeClicks++;else delivery.pointerDowns++;
    }
},{capture:true,signal:controller.signal});
// Return the validated element for a normal WebDriver click. A JavaScript
// button.click() only proves that a synthetic event was dispatched.
return {status:'ready',button,geometry,american,alternatePoint:point!==center,
    clickPoint:{...point,offsetX:point.x-center.x,offsetY:point.y-center.y}};
"""

CLICK_DELIVERY_SCRIPT = """
const result=window.__ttSelectionDelivery;
return result?{testId:result.testId,nativeClicks:result.nativeClicks,pointerDowns:result.pointerDowns}:null;
"""

# Verified against a live populated DraftKings slip: the stake box is
# betslip-wager-box-input (name="stake") and submission is betslip-place-bet-button.
# The button's label is dynamic (it reads "Accept Odds Changes" after a price move),
# so it is matched by test id, never by text.
MARKET_SECTION_SCRIPT = VISIBLE_UI_JS + """
const normalize=s=>s.replace(/[\\s\\u2013\\u2014-]+/g,' ').trim().toLowerCase();
const titles=(Array.isArray(arguments[0])?arguments[0]:[arguments[0]]).map(normalize);
return [...document.querySelectorAll('[data-testid="collapsible-wrapper"]')].filter(visibleUI)
    .filter(g=>titles.includes(normalize(g.querySelector('[data-testid="collapsible-header"]')?.textContent||''))).length===1;
"""

MARKET_TAB_STATE_SCRIPT = VISIBLE_UI_JS + """
const cfg=arguments[0],tabs=[...document.querySelectorAll('[role="tab"]')].filter(visibleUI);
const current=tabs.find(tab=>tab.getAttribute('aria-selected')==='true');
const label=tab=>(tab.innerText||tab.textContent||'').trim();
if(current&&(cfg.matchMarket||cfg.section==='Moneyline'?label(current)==='Match Lines':
    ['Player Props','Game Props'].includes(label(current))))return {tab:current};
const normalize=s=>s.replace(/[\\s\\u2013\\u2014-]+/g,' ').trim().toLowerCase();
const titles=(cfg.sections||[cfg.section]).map(normalize);
const hasSection=[...document.querySelectorAll('[data-testid="collapsible-wrapper"]')].filter(visibleUI)
    .filter(g=>titles.includes(normalize(g.querySelector('[data-testid="collapsible-header"]')?.textContent||''))).length===1;
if(current&&['Popular','Match Lines'].includes(label(current))&&hasSection)return {tab:current};
for(const name of cfg.labels){const tab=tabs.find(tab=>label(tab)===name);if(tab)return {tab};}
return {tab:null};
"""

NAVIGATION_CLICK_SCRIPT = VISIBLE_UI_JS + """
const element=arguments[0],label=arguments[1];
if(!visibleUI(element)||element.getAttribute('aria-disabled')==='true'||element.disabled)
    return {status:'waiting',reason:'navigation control is hidden or disabled'};
if(label&&((element.innerText||element.textContent||'').trim()!==label||element.getAttribute('role')!=='tab'))
    return {status:'waiting',reason:'market tab identity changed'};
element.scrollIntoView({block:'center',inline:'center',behavior:'instant'});
const r=element.getClientRects()[0],left=Math.max(0,r.left),right=Math.min(innerWidth,r.right),
    top=Math.max(0,r.top),bottom=Math.min(innerHeight,r.bottom);
const center={x:Math.floor((left+right)/2),y:Math.floor((top+bottom)/2)},points=[center];
for(const y of [.2,.8])for(const x of [.2,.5,.8])points.push({x:Math.floor(left+(right-left)*x),y:Math.floor(top+(bottom-top)*y)});
const point=left<right&&top<bottom?points.find(p=>element.contains(document.elementFromPoint(p.x,p.y))):null;
if(!point){const hit=document.elementFromPoint(center.x,center.y);return {status:'waiting',reason:'navigation control is obstructed',
    blocker:hit?{tag:hit.tagName,testId:hit.getAttribute('data-testid'),classes:typeof hit.className==='string'?hit.className:null}:null};}
return {status:'ready',alternatePoint:point!==center,clickPoint:{offsetX:point.x-center.x,offsetY:point.y-center.y}};
"""

SLIP_PANEL_SCRIPT = VISIBLE_UI_JS + """
const panels = [...document.querySelectorAll('[data-testid="betslip-content-wrapper"]')];
const shown = panels.filter(visibleUI);
const viewport={width:innerWidth,height:innerHeight};
if(innerWidth<1200||innerHeight<600)return {status:'viewport-too-small',viewport,
    reason:'desktop bet-slip layout requires at least 1200x600 CSS pixels'};
if (shown.length === 1) return {status: 'ready',viewport};
if (shown.length > 1) return {status: 'waiting', reason: 'multiple visible bet slips'};
if (!panels.length) return {status: 'missing'};
const headers = [...document.querySelectorAll('[data-testid="betslip-header"][role="button"][aria-controls]')]
    .filter(visibleUI).filter(header => header.getAttribute('aria-controls').split(/\\s+/)
        .some(id => { const controlled = document.getElementById(id);
            return controlled && panels.some(panel => controlled === panel || controlled.contains(panel)); }));
if (headers.length === 1 && headers[0].getAttribute('aria-expanded') === 'false') {
    return {status: 'openable', button: headers[0]};
}
if (headers.length === 1 && headers[0].getAttribute('aria-expanded') === 'true') {
    return {status:'waiting',reason:'waiting for the expanded bet-slip animation'};
}
if (panels.every(panel => panel.querySelector('[data-testid="betslip-empty-state-title"]'))) {
    return {status: 'empty'};
}
return {status: 'waiting', reason: 'waiting for the bet-slip panel to become visible'};
"""

LOAD_RECOVERY_SCRIPT = VISIBLE_UI_JS + LOAD_RECOVERY_JS

# Runs before selecting: a leg left over from earlier browsing would otherwise ride
# along with ours, so the slip is emptied (empty already, or via its confirmed
# clear-all action) before the click. A slip that is not mounted yet has no legs.
CLEAR_SLIP_SCRIPT = VISIBLE_UI_JS + """
const panels = [...document.querySelectorAll('[data-testid="betslip-content-wrapper"]')];
const shown = panels.filter(visibleUI);
if (!shown.length && (!panels.length || panels.every(panel => panel.querySelector('[data-testid="betslip-empty-state-title"]')))) {
    return {status: 'already-empty'};
}
if (shown.length !== 1) return {status: 'blocked'};
const slip = shown[0];
if (slip.querySelector('[data-testid="betslip-empty-state-title"]')) return {status: 'already-empty'};
const headers = [...document.querySelectorAll('[data-testid="betslip-header-clear-all-button"]')].filter(visibleUI);
if (headers.length !== 1) return {status: 'blocked'};
const header = headers[0];
header.click();
return {status: 'clearing'};
"""

CLEAR_SLIP_CONFIRM_SCRIPT = VISIBLE_UI_JS + """
const buttons = [...document.querySelectorAll('[data-testid="betslip-clear-all-confirm-btn"]')].filter(visibleUI);
if (buttons.length !== 1) return {status: 'waiting'};
const button = buttons[0];
button.click();
return {status: 'cleared'};
"""

SLIP_STATE_SCRIPT = VISIBLE_UI_JS + """
const allPanels = [...document.querySelectorAll('[data-testid="betslip-content-wrapper"]')];
const panels = allPanels.filter(visibleUI);
if (!panels.length && allPanels.length && allPanels.every(panel => panel.querySelector('[data-testid="betslip-empty-state-title"]'))) {
    return {slip: 'hidden-empty', empty: true, text: ''};
}
if (panels.length !== 1) return {slip: 'missing or ambiguous', empty: null, text: null};
const slip = panels[0];
return {slip: 'present', empty: !!slip.querySelector('[data-testid="betslip-empty-state-title"]'),
        text: slip.innerText.slice(0, 600)};
"""

# Preparation may stage the exact anticipated selection on the open slip before
# any edge is proven. Reading the staged selection IDs lets the submit path reuse
# that leg (place the size and submit) instead of clearing and re-clicking it,
# and lets idle maintenance clear it once the opportunity is gone.
SLIP_LEGS_SCRIPT = VISIBLE_UI_JS + BALANCE_READER_JS + EXACT_SLIP_READER_JS + """
if(arguments[0]?.exact)return publicSlipSnapshot();
const panels = [...document.querySelectorAll('[data-testid="betslip-content-wrapper"]')].filter(visibleUI);
if (panels.length !== 1) return {status: 'missing', legs: []};
const slip = panels[0];
if (slip.querySelector('[data-testid="betslip-empty-state-title"]')) return {status: 'empty', legs: []};
const legs = [...slip.querySelectorAll('[data-testid="betslip-selection-card-ex-button"]')]
    .map(remove => {
        try { return JSON.parse(remove.getAttribute('data-tracking') || '{}'); }
        catch (_) { return null; }
    })
    .filter(Boolean)
    .map(tracking => ({selectionId: tracking.selectionId ?? null,
        eventId: tracking.eventId ?? null, marketId: tracking.marketId ?? null}));
return {status: 'present', legs};
"""

# After the submit click DraftKings answers with a receipt or its own error text.
BET_RECEIPT_SCRIPT = LOCATION_READER_JS + VISIBLE_UI_JS + SUBMISSION_UI_JS + """
const locationState = readDkLocation();
const panels = [...document.querySelectorAll('[data-testid="betslip-content-wrapper"]')].filter(visibleUI);
const slip = panels.length === 1 ? panels[0] : null;
const receipt=freshSubmissionReceipt(slip),text=submissionText(slip);
const continuation=slip?submissionContinuation(slip):null;
return {...receipt,text:receipt.text.slice(0,400),continuation,
        failed:!continuation&&/bet (?:was )?(?:rejected|not placed)|wager (?:was )?(?:rejected|not placed)|unable to place (?:your |the )?bet|not available/i.test(text),
        transientLoadError:!!loadNotice(),locationBlocked:locationState.blocked,location:locationState};
"""

# DraftKings keeps serving scores, markets and pages to a signed-out browser, so
# nothing else in this loop can tell that the account has gone. The header can.
SIGNED_IN_SCRIPT = VISIBLE_UI_JS + """
const text = element => (element.innerText || element.textContent || '').replace(/\\s+/g, ' ').trim();
const visible = visibleUI;
const slips = [...document.querySelectorAll('[data-testid="betslip-content-wrapper"]')].filter(visible);
const submit = slips.flatMap(slip => [...slip.querySelectorAll('[data-testid="betslip-place-bet-button"]')]).filter(visible);
if (submit.some(button => /log\\s*in|sign\\s*in/i.test(text(button)))) {
    return {state: 'signed-out', evidence: 'visible bet-slip submit asks to sign in'};
}
const account = document.getElementById('standardized-header-account-menu');
const controls = [...document.querySelectorAll('button, a, [role="button"]')];
const login = controls.filter(visible).map(text).find(value => /^(?:sign\\s*up\\s*(?:or|\\/)\\s*)?(?:log\\s*in|sign\\s*in)$/i.test(value));
if (login) return {state: 'signed-out', label: login};
if (account && visible(account)) return {state: 'signed-in'};
const native=window.__playSpan?.playSpan;
if (window.__isLoggedIn===true && window.__canBet===true && native?.clientKey && native.grantType==='PlaySportsbook') {
    return {state:'signed-in',evidence:'native authenticated sportsbook grant'};
}
return {state: 'unknown'};
"""

# One same-origin request carrying the session cookies, from the page that is
# already open. It never touches the DOM and never navigates; the answer is
# stored for the next tick to read. The cache-busting parameter forces the
# request through to the origin instead of an edge-cached page shell.
KEEP_ALIVE_PING_SCRIPT = """
const url = new URL(location.href);
url.pathname = '/';
url.search = '';
url.searchParams.set('dk_keep_alive', String(Date.now()));
window.__dkKeepAlive = null;
fetch(url.toString(), {credentials: 'include', cache: 'no-store'})
    .then(response => { window.__dkKeepAlive = {status: response.status, ok: response.ok,
        retryAfter: response.headers.get('Retry-After'), at: Date.now()}; })
    .catch(() => { window.__dkKeepAlive = {status: 0, ok: false, at: Date.now()}; });
return true;
"""

KEEP_ALIVE_RESULT_SCRIPT = """
return window.__dkKeepAlive || null;
"""


# Protocol 2 execution surface. The plan arrives already validated; the browser
# only re-verifies identity at the last instant. No EV, probability or sizing
# arithmetic ever runs here — the server computed the floor and the stake.
PLAN_SLIP_READER_JS = VISIBLE_UI_JS + BALANCE_READER_JS + EXACT_SLIP_READER_JS
PLAN_SLIP_SNAPSHOT_SCRIPT = SLIP_LEGS_SCRIPT
UNFUNDED_PICK_REMOVAL_JS = """
const removeUnfundedPicks=keep=>{
let snapshot=exactSlipSnapshot();
const safe=()=>['present','empty'].includes(snapshot.status)&&snapshot.stagingSafe===true;
const blocked=()=>({status:'blocked',reason:snapshot.reason||'slip funding is not zero'});
if(!safe())return blocked();
const closed=snapshot.legs.filter(leg=>!keep(leg)),removed=[];
if(closed.length>128)return {status:'blocked',reason:'too many closed picks'};
for(const identity of closed){
    snapshot=exactSlipSnapshot();
    if(!safe())return blocked();
    const matches=snapshot.legs.filter(leg=>['eventId','marketId','selectionId'].every(k=>leg[k]===identity[k]));
    if(!matches.length)continue;
    if(matches.length!==1)return {status:'blocked',reason:'closed pick identity is ambiguous'};
    const leg=matches[0];
    if(keep(leg))continue;
    const controls=[...leg.root.querySelectorAll(removeSelector)].filter(visibleUI);
    if(controls.length!==1||controls[0].tagName!=='BUTTON'||controls[0].disabled||controls[0].getAttribute('aria-disabled')==='true')
        return {status:'blocked',reason:'closed pick removal is unavailable'};
    controls[0].click();removed.push(leg.selectionId);
}
snapshot=exactSlipSnapshot();
if(!safe())return blocked();
return {status:snapshot.legs.some(leg=>!keep(leg))?'waiting':'cleared',removed};
};
"""
PLAN_REMOVE_CLOSED_SCRIPT = PLAN_SLIP_READER_JS + UNFUNDED_PICK_REMOVAL_JS + 'return removeUnfundedPicks(leg=>!leg.closed);'
PLAN_SINGLE_SLIP_SCRIPT = PLAN_SLIP_READER_JS + UNFUNDED_PICK_REMOVAL_JS + """
const cfg=arguments[0];
return removeUnfundedPicks(leg=>cfg&&['eventId','marketId','selectionId'].every(key=>leg[key]===cfg[key]));
"""
PLAN_SLIP_READY_SCRIPT = LOCATION_READER_JS + PLAN_SLIP_READER_JS + """
const leg=readExactPlanLeg(arguments[0]);
return {ready:!readDkLocation().blocked&&leg.status==='ready'&&exactSlipSnapshot().stagingSafe===true};
"""
PLAN_CLEAR_STAKE_SCRIPT = PLAN_SLIP_READER_JS + """
const owned=window.__ttPlanStake,cfg=arguments[0];
if(!owned||owned.token!==cfg.stakeToken)return {cleared:true};
let input=owned.input;
if(!input.isConnected){
    const leg=readExactPlanLeg(cfg,true);
    if(leg.status!=='ready'||!leg.input)return {cleared:false};
    input=leg.input;
}
if(Number.isFinite(cfg.recoveryStake)){
    const leg=readExactPlanLeg(cfg,true);
    if(leg.status!=='ready'||leg.input!==input||!zeroStake(input)&&Number(input.value)!==cfg.recoveryStake)
        return {cleared:false};
}
writeSlipStake(input,'');
const cleared=input.value===''&&exactSlipSnapshot().stagingSafe===true&&
    [...document.querySelectorAll(stakeSelector)].every(zeroStake);
if(cleared)window.__ttPlanStake=null;
return {cleared};
"""

PLAN_ARM_READER_JS = LOCATION_READER_JS + PLAN_SLIP_READER_JS + """
const armPlanSlip = cfg => {
const locationState = readDkLocation();
if (locationState.blocked) return {status: 'waiting', locationBlocked: true,
    location: locationState, reason: locationState.message};
const notices = [...document.querySelectorAll('[role="alert"], [data-testid*="toast"]')]
    .filter(e => e.getClientRects().length).map(e => (e.innerText || '').trim().slice(0, 600)).filter(Boolean);
const leg=readExactPlanLeg(cfg);
if(leg.status!=='ready')return leg;
const {slip,input,button}=leg;
const label = (button.innerText || '').trim();
if (/log\\s*in|sign\\s*in/i.test(label)) return {status: 'waiting', signIn: true,
    slipVisible: true, submitVisible: true, placeText: label,
    reason: 'the visible submit control asks to sign in', notices};
const decimalOdds = leg.decimalOdds;
const balance = readAvailableBalance();
const common = {slipVisible: true, submitVisible: true, placeText: label, decimalOdds, balance,
        targetFingerprint:leg.fingerprint,
        placeDisabled: button.disabled || button.getAttribute('aria-disabled') === 'true',
        acceptsOddsChange: /accept\\s+(?:odds|price)\\s+changes?/i.test(label),
        odds: [...leg.root.querySelectorAll('[data-testid="betslip-odds-standard"]')]
            .filter(visibleUI).map(e => (e.innerText || '').trim()).filter(Boolean),
        slipText: leg.text, notices};
if (cfg.readOnly) {
    if (!cfg.expectedFingerprint && !zeroStake(input)) return {...common, status:'aborted',
        quoteRejected:true, reason:'the staged target stake is not zero'};
    if (balance.reason) return {...common, status: 'aborted', quoteRejected: true, reason: balance.reason};
    if (decimalOdds === null) return {...common, status: 'aborted', quoteRejected: true,
        reason: 'the slip price is unreadable'};
    return {...common, status: 'armed', stakeValue: input.value};
}
const stake = Number(cfg.stake);
if (!Number.isFinite(stake) || stake < .01) return {...common, status: 'aborted',
    quoteRejected: true, reason: 'the authorized stake is invalid'};
const stakeWritten = !input.value.trim() || Number(input.value) !== stake;
if (stakeWritten) {
    if(cfg.continuation)return {...common,status:'aborted',reason:'continuation stake changed; no rearming after submission'};
    window.__ttPlanStake={token:cfg.stakeToken,input};
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    setter.call(input, String(stake));
    input.dispatchEvent(new Event('input', {bubbles: true}));
    input.dispatchEvent(new Event('change', {bubbles: true}));
}
const stakePending = /enter\\s+(?:wager|stake)\\s+amount/i.test(label);
const submitReady = /place\\s*bet|accept\\s+(?:odds|price)\\s+changes?/i.test(label);
return {...common, status: stakeWritten || stakePending || !submitReady ? 'waiting' : 'armed',
        reason: stakePending ? 'waiting for DraftKings to process the stake' :
            !submitReady ? 'waiting for a place-bet or accept-price control' : null,
        stakeValue: input.value, stakeWritten};
};
"""

PLAN_ARM_SCRIPT = PLAN_ARM_READER_JS + "return armPlanSlip(arguments[0]);"

# One local WebDriver command watches rendering/label/disabled changes. No site
# requests, selection clicks or submit clicks occur in this readiness watcher.
WAIT_FOR_PLAN_ARM_SCRIPT = PLAN_ARM_READER_JS + """
const cfg = arguments[0], done = arguments[arguments.length - 1];
const start = performance.now();
const timeoutMs=Math.max(0,Math.min(250,cfg.timeoutMs||0,cfg.validUntil-Date.now()));
let observer, deadline, fallback, scheduled, last, finished = false, checks = 0, stakeWrites = 0;
const finish = (result, timedOut = false) => {
    if (finished) return;
    finished = true;
    observer?.disconnect();
    clearTimeout(deadline); clearTimeout(scheduled); clearInterval(fallback);
    done({...result, readiness: {waitMs: performance.now() - start, checks, stakeWrites, timedOut}});
};
const read = () => {
    checks++;
    const result = armPlanSlip(cfg);
    if (result.stakeWritten) stakeWrites++;
    last=result;
    return result;
};
const check = () => {
    clearTimeout(scheduled);
    scheduled = null;
    if (finished) return;
    const result = read();
    if (result.empty && performance.now()-start >= emptyDeadline) {
        finish({...result,deliveryTimedOut:true});
        return;
    }
    if (result.signIn || result.locationBlocked || result.status === 'aborted'
            || result.status === 'armed' && (cfg.readOnly || !result.placeDisabled)) finish(result);
};
observer = new MutationObserver(() => {
    if (!finished && scheduled === null) scheduled = setTimeout(check, 0);
});
scheduled = null;
observer.observe(document.documentElement, {subtree: true, childList: true, attributes: true, characterData: true});
deadline = setTimeout(() => finish(last||{status:'waiting',notReady:true}, true), timeoutMs);
const emptyDeadline = Math.min(timeoutMs,cfg.emptyTimeoutMs ?? timeoutMs);
fallback = setInterval(check, cfg.fallbackMs);
check();
"""

CONFIRM_PLAN_SCRIPT = LOCATION_READER_JS + PLAN_SLIP_READER_JS + SUBMISSION_UI_JS + """
if(loadNotice())return {status:'aborted',uiRejected:true,notReady:true,reason:'provider load error before submission'};
const locationState = readDkLocation();
if (locationState.blocked) return {status: 'aborted', locationBlocked: true,
    location: locationState, reason: locationState.message};
const cfg = arguments[0] || {};
if (!Number.isFinite(cfg.stake) || cfg.stake < .01
        || !Number.isFinite(cfg.minDecimal) || cfg.minDecimal <= 1) {
    return {status: 'aborted', planRejected: true, reason: 'the authorized stake or price floor is invalid'};
}
const leg=readExactPlanLeg(cfg);
if(leg.status!=='ready')return leg;
const {slip}=leg;
if(cfg.continuation&&JSON.stringify(window.__ttSubmitFlow?.planIdentity)!==JSON.stringify({
    eventId:cfg.eventId,marketId:cfg.marketId,selectionId:cfg.selectionId,selectionLabel:cfg.selectionLabel,
    marketName:cfg.marketName,selectionLine:cfg.selectionLine}))
    return {status:'aborted',uiRejected:true,reason:'continuation target identity changed'};
const continuationError=checkSubmissionContinuation(cfg,slip);
if(continuationError)return continuationError;
const limit=cfg.continuation?.kind==='stake-limit'?stakeLimitPrompt():null;
if(cfg.continuation?.kind==='stake-limit'&&(!limit||limit.maximum!==cfg.providerStakeLimit))
    return {status:'aborted',reason:'provider maximum stake changed'};
const surface=limit?.root||slip;
// A detached limit dialog has no independently verified contract identity.
if(limit&&limit.root!==leg.root)return {status:'aborted',uiRejected:true,reason:'unsupported unscoped stake-limit confirmation'};
const normalizeSubmission=s=>s.toLowerCase().replace(/[łøđ]/g,c=>({ł:'l',ø:'o',đ:'d'})[c])
    .normalize('NFKD').replace(/\\p{M}/gu,'').replace(/[^\\p{L}\\p{N}]+/gu,' ').trim();
const containsSubmission=value=>(' '+normalizeSubmission(submissionText(leg.root))+' ').includes(' '+normalizeSubmission(value)+' ');
const marketNames=cfg.marketName==='Moneyline'?['Moneyline','Match Winner']:[cfg.marketName];
if(cfg.selectionLabel&&(!containsSubmission(cfg.selectionLabel)||!marketNames.some(containsSubmission)))
    return {status:'aborted',uiRejected:true,reason:'confirmation does not match the selected player and market'};
if(cfg.selectionLine!==undefined&&cfg.selectionLine!==null){
    const text=submissionText(leg.root).replace(/\\u2212|\\u2013/g,'-').replace(/\\$\\s*[\\d,.]+/g,' ');
    const numbers=[...text.matchAll(/(?<![\\d.+-])([+\\-]?\\d+(?:\\.\\d+)?)(?![\\d.])/g)].map(m=>Number(m[1]));
    if(!numbers.some(value=>Math.abs(value-cfg.selectionLine)<1e-9))
        return {status:'aborted',uiRejected:true,reason:'confirmation handicap or total line changed'};
}
if(loadNotice())return {status:'aborted',uiRejected:true,reason:'provider page load error has not cleared'};
const buttons = limit?[limit.button]:[...surface.querySelectorAll('button[data-testid="betslip-place-bet-button"]')].filter(visibleUI);
if (buttons.length !== 1) return {status: 'aborted', uiRejected: true, reason: 'place-bet button is hidden or ambiguous'};
const button = buttons[0];
if (button.disabled || button.getAttribute('aria-disabled') === 'true') {
    return {status: 'aborted', reason: 'place-bet button is disabled'};
}
// Re-read the label between validation and the click: losing the session swaps
// this control for a sign-in button that must never be clicked.
const testId = (button.getAttribute('data-testid') || '');
if (!limit&&testId !== 'betslip-place-bet-button') return {status: 'aborted', reason: 'submit control changed identity'};
const label = (button.innerText || '').trim();
const limitButtonAmount=limit?label.match(/^place\\s*bet\\s*\\$\\s*((?:\\d{1,3}(?:,\\d{3})+|\\d+)\\.\\d{2})(?!\\d)/i):null;
if(limitButtonAmount&&Math.abs(Number(limitButtonAmount[1].replace(/,/g,''))-cfg.stake)>.001)
    return {status:'aborted',reason:'provider confirmation button stake is inconsistent'};
if (/log\\s*in|sign\\s*in/i.test(label)) return {status: 'aborted', signIn: true, reason: 'submit control now asks to sign in'};
if (!/place\\s*bet|accept\\s+(?:odds|price)\\s+changes?/i.test(label)) {
    return {status: 'aborted', uiRejected: true, reason: 'submit control label changed'};
}
const odds = [...leg.root.querySelectorAll('[data-testid="betslip-odds-standard"]')]
    .filter(visibleUI).map(e => (e.innerText || '').trim()).filter(Boolean);
const decimals = odds.map(text => {
    const cleaned = text.replace(/\\u2212|\\u2013/g, '-');
    if (!/^[+\\-]?\\d+$/.test(cleaned)) return null;
    const value = Number(cleaned);
    if (!value) return null;
    return 1 + (value < 0 ? 100 / -value : value / 100);
});
if (!decimals.length || decimals.some(d => !Number.isFinite(d) || d <= 1)
    || decimals.some(d => Math.abs(d - decimals[0]) > 1e-9)) {
    return {status: 'aborted', edgeRejected: true, reason: 'slip price is missing, unreadable or ambiguous'};
}
const decimalOdds = decimals[0];
// The plan is the sole authority: the price must hold its authorized floor and
// the stake must be exactly the authorized amount, or nothing is submitted.
if (decimalOdds < cfg.minDecimal) {
    return {status: 'aborted', edgeRejected: true, odds, decimalOdds,
            minDecimal: cfg.minDecimal, echo: cfg.echo,
            reason: 'the final slip price is below the authorized price floor'};
}
const inputs = [leg.input];
if (inputs.length !== 1 || !inputs[0].value.trim() || !Number.isFinite(Number(inputs[0].value))) {
    return {status: 'aborted', planRejected: true, reason: 'final slip stake is missing or ambiguous'};
}
if (Number(inputs[0].value) !== cfg.stake) {
    return {status: 'resize', reason: 'the authorized stake changed on the slip'};
}
const submitKind = limit?'accept-stake-limit':submissionKind(button);
recordSubmissionClick(cfg,slip,surface,button,submitKind,limit?.maximum);
window.__ttSubmitFlow.planIdentity={eventId:cfg.eventId,marketId:cfg.marketId,selectionId:cfg.selectionId,
    selectionLabel:cfg.selectionLabel,marketName:cfg.marketName,selectionLine:cfg.selectionLine};
const submittedAt = new Date().toISOString();
if(!Number.isFinite(cfg.validUntil)||Date.now()>=cfg.validUntil)
    return {status:'aborted',uiRejected:true,reason:'plan deadline expired before submit'};
const finalNative=window.__playSpan?.playSpan;
if(window.__isLoggedIn!==true||window.__canBet!==true||!finalNative?.clientKey||finalNative.grantType!=='PlaySportsbook')
    return {status:'aborted',signIn:true,reason:'native wagering permission expired before submit'};
button.click();
return {status: 'submitted', placeText: label, odds, decimalOdds,
        minDecimal: cfg.minDecimal, stake: cfg.stake, echo: cfg.echo,
        submitKind, submittedAt};
"""


class BetSlipError(RuntimeError):
    """A bet could not be placed safely; betting stops for the session."""


class SignInRequired(BetSlipError):
    """DraftKings wants a sign-in, so no wager can be submitted until there is one."""


class LocationRequired(BetSlipError):
    """No submit ran; the real location check needs attention in the browser."""


class BetNotSubmitted(BetSlipError):
    """The attempt failed before submission; later sets and games can still bet."""

    def __init__(self, message, comparison=None, *, not_ready=False, window_expired=False):
        super().__init__(message)
        self.comparison = comparison
        self.not_ready = not_ready
        self.window_expired=window_expired


def signed_in_state(driver):
    """'signed-in', 'signed-out' or 'unknown', read from the sportsbook header."""
    result = driver.execute_script(SIGNED_IN_SCRIPT) or {}
    state = result.get("state")
    return state if state in ("signed-in", "signed-out") else "unknown"


def auth_change(bettor, state):
    """Apply a header reading to the bettor; return the line to print, if any."""
    if state == "signed-out":
        if "auth" not in bettor.pause_reasons:
            bettor.pause("DraftKings is signed out")
            tail = ("Betting is paused; sign in again in this same browser window to resume."
                    if bettor.enabled else "Sign in again in this same browser window to resume.")
            return ("DraftKings is signed out: the header shows Log In, so no wager can be "
                    "submitted. " + tail)
        return None
    if state == "signed-in" and "auth" in bettor.pause_reasons:
        bettor.resume()
        if bettor.paused:
            return f"DraftKings is signed in again; betting remains paused: {bettor.paused}."
        return ("DraftKings is signed in again; betting resumed." if bettor.enabled else
                "DraftKings is signed in again.")
    return None


def alert_auth_required():
    mark_terminal_auth_required()
    from draftkings_login import alert_failure
    alert_failure()
    print('SIGN IN REQUIRED: betting is PAUSED. Complete login/SMS in the existing bot Chrome; '
          'monitoring alone does not mean orders are enabled.',flush=True)


def keep_alive_line(result):
    """The one-line verdict for a finished keep-alive ping; None when there is none."""
    if not result:
        return None
    status = result.get("status")
    if result.get("ok"):
        return (f"Keep-alive: the previous ping reached the sportsbook (HTTP {status}). "
                "This response does not confirm that the account is signed in.")
    if status:
        return f"Keep-alive: the previous ping answered HTTP {status} instead of 200."
    return "Keep-alive: the previous ping did not reach the sportsbook."


def click_selection(driver, event, outcome, market=None, validate=None, timeout=SELECTION_CLICK_TIMEOUT,*,min_decimal=None,observe_price=None):
    """Click the odds button the reveal already validated, rechecking it first."""
    timestamps = [datetime.fromisoformat(row["receivedAt"]).timestamp()
                  for row in (event, market or {}) if row.get("receivedAt")]
    config = {
        "testId": f"component-builder-market-button-{event['eventId']}-{outcome['selectionId']}",
        "selectionId": outcome["selectionId"], "label": outcome["label"],
        "validUntil": (min(timestamps) + 90) * 1000 if timestamps else None}
    deadline = time.monotonic() + timeout
    last = None
    checks = 0
    while True:
        if validate is not None and not validate():
            return {'status':'aborted','reason':'the score, price or entry window changed before selection',
                    'positionChecks':checks,'clickFailure':last}
        result = driver.execute_script(CLICK_SCRIPT, config) or {}
        checks += 1
        status = result.get('status')
        if status not in ('ready','blocked'):
            return result
        if status=='ready':
            if min_decimal is not None:
                quote=american_odds(result.get('american'))
                if quote is None:return {'status':'aborted','notReady':True,'reason':'selection price is unreadable'}
                if quote<min_decimal:return {'status':'aborted','windowExpired':True,'reason':'conservative EV decreased'}
                if observe_price is not None and not observe_price(quote):
                    return {'status':'aborted','windowExpired':True,'reason':'conservative EV decreased'}
            button = result.get('button')
            if button is None:
                return {'status':'aborted','reason':'validated selection button disappeared'}
            try:
                if result.get('alternatePoint'):
                    point = result['clickPoint']
                    ActionChains(driver).move_to_element_with_offset(
                        button,point['offsetX'],point['offsetY']).click().perform()
                else:
                    button.click()
                delivery = driver.execute_script(CLICK_DELIVERY_SCRIPT) or {}
                delivered = delivery.get('nativeClicks', 0) > 0
                if not delivered:
                    # Site capture listeners can prevent our observer from
                    # seeing an otherwise delivered event. DOM identity wins.
                    current = driver.execute_script(CLICK_SCRIPT, {**config, 'inspectOnly': True}) or {}
                    if current.get('status') == 'selected':
                        return {**current, 'delivery': delivery, 'positionChecks': checks}
                return {'status':'clicked' if delivered else 'not-delivered',
                        'reason':None if delivered else 'no trusted click reached the exact selection',
                        'delivery':delivery,'geometry':result.get('geometry'),
                        'alternatePoint':result.get('alternatePoint',False),'positionChecks':checks}
            except (ElementClickInterceptedException,StaleElementReferenceException) as error:
                delivery = driver.execute_script(CLICK_DELIVERY_SCRIPT) or {}
                if delivery.get('nativeClicks',0)>0:
                    return {'status':'clicked','delivery':delivery,'positionChecks':checks}
                fresh = driver.execute_script(CLICK_SCRIPT, {**config, 'inspectOnly': True}) or {}
                last = {'errorType': type(error).__name__, 'geometry': fresh.get('geometry', result.get('geometry'))}
        else:
            last={'geometry':result.get('geometry'),'reason':result.get('reason')}
        if time.monotonic() >= deadline:
            return {'status':'aborted','reason':'selection remained obstructed after bounded native-click recovery',
                    'clickFailure':last,'positionChecks':checks}
        time.sleep(.05)


def save_slip_capture(payload):
    """Best-effort forensics; a locked or full disk must never stop a wager."""
    try:
        SLIP_CAPTURE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as error:
        print(f"Could not write {SLIP_CAPTURE.name} ({type(error).__name__}).", flush=True)


def ensure_slip_open(driver, timeout=3, allow_empty=True):
    """Open the site's real collapsed accordion with one trusted header click."""
    deadline, clicked = time.monotonic() + timeout, False
    fitted = False
    for _ in range(math.ceil(timeout / .05) + 1):
        result = driver.execute_script(SLIP_PANEL_SCRIPT) or {}
        driver._tt_slip_readiness = {key:value for key,value in result.items() if key != 'button'}
        if result.get('status') == 'viewport-too-small' and not fitted:
            from browser_viewport import configure_fixed_viewport, fit_headed_window
            if getattr(driver, '_tt_fixed_viewport', False) is True:
                configure_fixed_viewport(driver)
            else:
                fit_headed_window(driver)
            fitted = True
            continue
        if result.get("status") == "ready" or allow_empty and result.get("status") in ("missing", "empty"):
            return True
        if result.get("status") == "openable" and not clicked:
            button = result.get("button")
            if button is None:
                return False
            button.click()
            clicked = True
        if time.monotonic() >= deadline:
            break
        time.sleep(.05)
    return False


def clear_slip(driver, timeout=6):
    """Empty the bet slip so the only leg on it is the one this run clicks."""
    result = driver.execute_script(CLEAR_SLIP_SCRIPT) or {}
    if result.get("status") == "already-empty":
        return True
    if result.get("status") != "clearing":
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        confirmed = driver.execute_script(CLEAR_SLIP_CONFIRM_SCRIPT) or {}
        if confirmed.get("status") == "cleared":
            state = driver.execute_script(SLIP_STATE_SCRIPT) or {}
            if state.get("empty"):
                return True
        time.sleep(0.25)
    return False


def read_slip_legs(driver):
    """Selection IDs currently on the one visible slip; 'unknown' when hidden."""
    result = driver.execute_script(SLIP_LEGS_SCRIPT)
    result = result if isinstance(result, dict) else {}
    status = result.get("status")
    return {"status": status if status in ("empty", "present") else "unknown",
            "legs": [leg.get("selectionId") for leg in result.get("legs") or []
                     if isinstance(leg, dict)]}


def read_plan_slip(driver):
    """Non-wager snapshot of tracked legs and whether all stake boxes are zero."""
    result = driver.execute_script(PLAN_SLIP_SNAPSHOT_SCRIPT, {"exact": True})
    return result if isinstance(result, dict) else {"status": "unsupported", "stagingSafe": False, "legs": []}


def remove_closed_picks(driver):
    """Remove only exact native closed singles while the entire slip is unfunded."""
    if any(getattr(driver, flag, False) is True for flag in ('_tt_plan_uncertain', '_tt_plan_stopped')):
        return {'status': 'blocked', 'reason': 'a previous submission is unresolved'}
    result = driver.execute_script(PLAN_REMOVE_CLOSED_SCRIPT)
    return result if isinstance(result, dict) else {'status': 'blocked', 'reason': 'closed pick removal is unverified'}


def prepare_single_slip(driver,target=None):
    if any(getattr(driver,flag,False) is True for flag in ('_tt_plan_uncertain','_tt_plan_stopped')):
        return {'status':'blocked','reason':'a previous submission is unresolved'}
    result=driver.execute_script(PLAN_SINGLE_SLIP_SCRIPT,target)
    return result if isinstance(result,dict) else {'status':'blocked','reason':'single-slip cleanup is unverified'}


def plan_slip_ready(driver, event, market, plan):
    """Whether the exact unstaked leg can execute without any board navigation."""
    if getattr(driver, "_tt_plan_uncertain", False) is True or getattr(driver, "_tt_plan_stopped", False) is True:
        return False
    try:
        offset = getattr(driver, '_ember_server_offset', 0)
        config = plan_slip_config(event, market, plan, server_offset=offset if type(offset) in (int, float) else 0)
    except (KeyError, TypeError, ValueError):
        return False
    result = driver.execute_script(PLAN_SLIP_READY_SCRIPT, config)
    return isinstance(result, dict) and result.get("ready") is True


def _authorize_values(authorize_values, quote):
    """Values the plan executes with, or a denial the caller records verbatim."""
    values = authorize_values(quote)
    if not isinstance(values, dict):
        return {"denied": "authorization_denied"}
    if "denied" in values:
        return values
    stake, floor = values.get("stake"), values.get("min_decimal")
    if (type(stake) not in (int, float) or not math.isfinite(stake) or stake < .01
            or type(floor) not in (int, float) or not math.isfinite(floor) or floor <= 1):
        return {"denied": "authorization_denied"}
    return values


def place_plan(driver, event, market, plan, outcome, authorize_values, validate=None, timing=None, *, deadline_budget=None,observe_price=None):
    """Execute one validated server plan by comparison; no strategy math runs here.

    authorize_values receives each observed slip quote (balance + decimal odds)
    and returns the authoritative {"stake", "min_decimal", "echo"} — or
    {"denied": reason} when the server withholds the wager. The browser never
    re-derives a stake or a price floor; it only verifies them at the last instant.
    deadline_budget is an optional remaining dispatch budget in seconds. DOM waits
    are capped at 250 ms; a submitted wager still requires receipt reconciliation.
    """
    trace = timing or ExecutionTiming(event, market)
    if getattr(driver, "_tt_plan_uncertain", False) is True or getattr(driver, "_tt_plan_stopped", False) is True:
        return {"status": "stopped", "reason": "a previous plan submission is uncertain; reconcile before resuming",
                "submitted": False, "submissionState": "uncertain" if getattr(driver, "_tt_plan_uncertain", False) is True
                else "not-submitted", "retryable": False, "notReady": False}
    transaction = {"uncertain": False, "touched": False, "lost": False}
    driver._tt_plan_transaction = transaction
    result = None
    try:
        offset = getattr(driver, '_ember_server_offset', 0)
        transaction["target"] = plan_slip_config(event, market, plan, outcome,
            server_offset=offset if type(offset) in (int, float) else 0)
        transaction["target"]["stakeToken"] = str(time.time_ns())
        if deadline_budget is not None:
            if type(deadline_budget) not in (int, float) or not math.isfinite(deadline_budget) or deadline_budget < 0:
                raise ValueError("deadline_budget must be finite nonnegative seconds")
            transaction["target"]["validUntil"] = min(transaction["target"]["validUntil"],
                                                       (time.time() + deadline_budget) * 1000)
        transaction["expires"] = time.monotonic() + max(0, transaction["target"]["validUntil"] / 1000 - time.time())
        result = _place_plan_once(driver, event, market, plan, outcome, authorize_values, validate, trace, transaction,observe_price)
    except FATAL_BROWSER_ERRORS:
        transaction["lost"] = True
        driver._tt_plan_stopped = True
        raise
    except (KeyError, TypeError, ValueError) as error:
        result = {"status": "unconfirmed" if transaction["uncertain"] else "skipped",
                "reason": str(error), "submitted": transaction["uncertain"]}
    except SignInRequired as error:
        result = {"status": "paused", "reason": str(error), "eventId": event["eventId"],
                "market": market["name"], "label": outcome["label"], "submitted": False}
    except LocationRequired as error:
        result = {"status": "paused", "reason": str(error), "eventId": event["eventId"],
                "market": market["name"], "label": outcome["label"], "submitted": False}
    except BetNotSubmitted as error:
        result = {"status": "skipped", "reason": str(error), "eventId": event["eventId"],
                "market": market["name"], "label": outcome["label"], "submitted": False,
                 "comparison": error.comparison, "notReady": error.not_ready,'windowExpired':error.window_expired}
    except (BetSlipError, WebDriverException) as error:
        result = {"status": "unconfirmed" if transaction["uncertain"] else "stopped",
                "reason": f"{type(error).__name__}: {error}",
                "eventId": event["eventId"], "market": market["name"], "submitted": transaction["uncertain"]}
    finally:
        if transaction["uncertain"]:
            driver._tt_plan_uncertain = True
        if transaction["touched"] and not transaction["uncertain"] and not transaction["lost"]:
            try:
                cleared = driver.execute_script(PLAN_CLEAR_STAKE_SCRIPT, transaction["target"]) or {}
            except FATAL_BROWSER_ERRORS:
                driver._tt_plan_stopped = True
                raise
            except WebDriverException:
                cleared = {}
            if cleared.get("cleared") is not True:
                driver._tt_plan_stopped = True
                result = {"status": "stopped", "submitted": False, "cleanupFailed": True,
                          "reason": "the target stake could not be cleared; stop before another signal"}
    if result is not None:
        if transaction["uncertain"]:
            result.update(status="unconfirmed", submitted=True)
        result["submissionState"] = "uncertain" if transaction["uncertain"] else (
            "placed" if result.get("status") == "placed" else "not-submitted")
        result.setdefault("notReady", False)
        result["retryable"] = result["notReady"] and result["submissionState"] == "not-submitted"
        result["timing"] = trace.snapshot(result.get("status"))
    return result


def _plan_wait_ms(transaction):
    remaining = min(transaction["expires"] - time.monotonic(),
                    transaction["target"]["validUntil"] / 1000 - time.time())
    if remaining <= 0:
        raise BetNotSubmitted("plan deadline or dispatch budget expired",not_ready=transaction['target'].get('nonDecreasing') is True)
    return min(PLAN_READY_TIMEOUT, SLIP_ARM_TIMEOUT, remaining) * 1000


def _place_plan_once(driver, event, market, plan, outcome, authorize_values, validate, trace, transaction,observe_price=None):
    target = transaction["target"]
    def observe(result):
        price=result.get('decimalOdds')
        if observe_price is not None and type(price) in (int,float) and math.isfinite(price) and price>1:
            if not observe_price(price):raise BetNotSubmitted('conservative EV decreased',window_expired=True)
            target['minDecimal']=max(target['minDecimal'],price)
        return result
    if time.time() * 1000 >= target["validUntil"]:
        raise BetNotSubmitted("plan deadline expired")
    if validate is not None and not trace.call("planLiveValidation", validate):
        return {"status": "skipped", "reason": "the live authorization expired before the plan",
                "submitted": False, "denied": "authorization_denied"}
    if trace.call("planSignedInCheck", signed_in_state, driver) == "signed-out":
        raise SignInRequired("the header shows Log In before the plan was executed")
    location = trace.call("planLocationCheck", location_status, driver)
    if location.get("blocked"):
        raise LocationRequired(location.get("message") or location["state"])
    if not trace.call("planLoadRecovery", recover_load_notice, driver, LOAD_RECOVERY_SCRIPT, timeout=0):
        raise BetNotSubmitted("DraftKings page load error did not clear before submission", not_ready=True)
    if not trace.call("planSlipOpen", ensure_slip_open, driver, timeout=_plan_wait_ms(transaction) / 1000):
        raise BetNotSubmitted("the bet-slip accordion did not open", not_ready=True)
    snapshot = trace.call("planSlipLegs", read_plan_slip, driver)
    if 'window' in plan:
        cleared=trace.call('planSingleSlip',prepare_single_slip,driver,target)
        if cleared.get('status')!='cleared':raise BetNotSubmitted('single-slip cleanup is not ready',not_ready=True)
        snapshot=trace.call('planSlipLegs',read_plan_slip,driver)
    if snapshot.get('stagingSafe') is True and any(leg.get('closed') is True for leg in snapshot.get('legs', [])):
        removed = trace.call('planClosedPicks', remove_closed_picks, driver)
        if removed.get('status') != 'cleared':
            raise BetNotSubmitted('closed picks could not be removed', not_ready=True)
        snapshot = trace.call('planSlipLegs', read_plan_slip, driver)
    if snapshot.get("status") not in ("empty", "present") or not snapshot.get("stagingSafe"):
        raise BetNotSubmitted(snapshot.get("reason") or "the staged slip is unsupported or has stakes")
    staged = any(all(leg.get(key) == target[key] for key in ("eventId", "marketId", "selectionId"))
                 for leg in snapshot.get("legs", []))
    if snapshot.get("status") == "present" and not staged:
        raise BetNotSubmitted("the exact target is absent from the staged slip", not_ready=True)
    values = armed = None
    for attempt in (1,):
        clicked = ({"status": "selected", "reason": "exact staged leg"} if staged else
                   trace.call("planSelectionClick", click_selection, driver, event, outcome, market, validate,
                               timeout=_plan_wait_ms(transaction) / 1000,
                               **({'min_decimal':plan['min_decimal']} if 'window' in plan else {}),
                               **({'observe_price':observe_price} if observe_price is not None else {})))
        if not clicked or clicked.get("status") not in ("clicked", "selected", "not-delivered"):
            raise BetNotSubmitted((clicked or {}).get("reason", "selection click failed"))
        if clicked.get("status") == "not-delivered":
            armed = {"status": "waiting", "empty": True, "reason": clicked["reason"]}
        else:
            if not staged and not trace.call("planSelectedSlipOpen", ensure_slip_open, driver, allow_empty=False,
                                             timeout=_plan_wait_ms(transaction) / 1000):
                raise BetNotSubmitted("the selected bet slip did not become visible", not_ready=True)
            quote = observe(trace.call("planSlipQuote", driver.execute_async_script, WAIT_FOR_PLAN_ARM_SCRIPT,
                               {**target, "readOnly": True, "timeoutMs": _plan_wait_ms(transaction),
                                "emptyTimeoutMs": 0 if staged else _plan_wait_ms(transaction),
                                 "fallbackMs": SLIP_ARM_INTERVAL * 1000}) or {})
            if quote.get("signIn"):
                raise SignInRequired(f"the visible submit control asks to sign in ({quote.get('placeText')!r})")
            if quote.get("locationBlocked"):
                raise LocationRequired(quote.get("reason", "location verification required"))
            if quote.get("status") == "aborted":
                raise BetNotSubmitted(quote.get("reason", "the slip quote could not be read"),
                                      {"slipDecimal": quote.get("decimalOdds"), "balance": quote.get("balance")},
                                      not_ready=quote.get("notReady") is True,window_expired=quote.get('windowExpired') is True)
            if quote.get("status") != "armed":
                raise BetNotSubmitted(quote.get("reason", "the slip quote did not become ready"), not_ready=True)
            if (not isinstance(quote.get("targetFingerprint"), str) or not quote["targetFingerprint"]
                    or type(quote.get("decimalOdds")) not in (int, float)
                    or not math.isfinite(quote["decimalOdds"]) or quote["decimalOdds"] <= 1):
                raise BetNotSubmitted("the exact leg quote evidence is incomplete")
            values = _authorize_values(authorize_values, quote)
            if "denied" in values:
                return {"status": "skipped", "reason": f"the server withheld the wager ({values['denied']})",
                        "submitted": False, "denied": values["denied"],
                        "notReady": values["denied"] == "authorization_unavailable"}
            transaction['authorized'] = values
            target.update(expectedFingerprint=quote.get("targetFingerprint"), expectedDecimal=quote.get("decimalOdds"))
            transaction["touched"] = True
            armed = observe(trace.call("planSlipArming", driver.execute_async_script, WAIT_FOR_PLAN_ARM_SCRIPT,
                               {**target, "stake": values["stake"], "timeoutMs": _plan_wait_ms(transaction),
                                 "fallbackMs": SLIP_ARM_INTERVAL * 1000}) or {})
        if armed.get("signIn"):
            raise SignInRequired(f"the visible submit control asks to sign in ({armed.get('placeText')!r})")
        if armed.get("locationBlocked"):
            raise LocationRequired(armed.get("reason", "location verification required"))
        save_slip_capture({"stage": "plan_arming", "eventId": event["eventId"], "market": market["name"],
                           "label": outcome["label"], "selectionId": outcome["selectionId"],
                           "selectionAttempt": attempt, "click": clicked, "armed": armed,
                           "authorized": {key: values.get(key) for key in ("stake", "min_decimal")} if values else None,
                           "capturedAt": datetime.now(timezone.utc).isoformat()})
        if armed.get("status") == "armed" and not armed.get("placeDisabled"):
            break
        raise BetNotSubmitted(armed.get("reason") or "bet slip did not arm",
                               not_ready=armed.get("status") == "waiting" or armed.get("notReady") is True
                               or armed.get("placeDisabled") is True,window_expired=armed.get('windowExpired') is True)
    if armed is None or values is None:
        raise BetNotSubmitted("the plan did not arm")
    label = armed.get("placeText") or ""
    if re.search(r"log\s*in|sign\s*in", label, re.I):
        raise SignInRequired(f"the submit control asks to sign in ({label!r})")
    if not re.search(r"place\s*bet|accept\s+(?:odds|price)\s+changes?", label, re.I):
        raise BetSlipError(f"the submit control has an unexpected label ({label!r})")
    if f" {name_key(outcome['label'])} " not in f" {name_key(armed['slipText'])} ":
        raise BetSlipError("bet slip leg does not name the selected player")
    market_labels = ("Moneyline", "Match Winner") if market.get("name") == "Moneyline" else (market["name"],)
    if not any(f" {name_key(text)} " in f" {name_key(armed['slipText'])} " for text in market_labels):
        raise BetSlipError("bet slip leg does not name the market")
    if armed.get("acceptsOddsChange") and (armed.get("decimalOdds") is None
                                            or armed["decimalOdds"] < values["min_decimal"]):
        raise BetNotSubmitted("the changed slip price is below the authorized price floor")
    if armed.get("placeDisabled"):
        raise BetSlipError("place-bet button is disabled")
    confirmed = None
    for resize_attempt in range(3):
        _plan_wait_ms(transaction)
        if validate is not None and not trace.call("planFinalLiveValidation", validate):
            raise BetNotSubmitted("the live authorization expired before submission")
        transaction["uncertain"] = True
        driver._tt_plan_uncertain = True
        confirmed = trace.call("planFinalSubmit", driver.execute_script, CONFIRM_PLAN_SCRIPT,
                               {**target, "stake": values["stake"], "minDecimal": values["min_decimal"],
                                "echo": values.get("echo"), "selectionLabel": outcome["label"],
                                "marketName": market["name"],
                                 "selectionLine": (plan.get("specification") or {}).get("line")}) or {}
        transaction["uncertain"] = confirmed.get("status") not in ("aborted", "resize")
        driver._tt_plan_uncertain = transaction["uncertain"]
        if not transaction['uncertain']:observe(confirmed)
        if confirmed.get("status") != "resize":
            break
        if resize_attempt == 2:
            raise BetNotSubmitted("the authorized stake kept changing before submission")
        quote = trace.call("planResizeQuote", driver.execute_async_script, WAIT_FOR_PLAN_ARM_SCRIPT,
                           {**target, "readOnly": True, "timeoutMs": _plan_wait_ms(transaction), "fallbackMs": 50}) or {}
        if quote.get("status") != "armed" or quote.get("placeDisabled"):
            raise BetNotSubmitted("the changed stake did not re-read")
        values = _authorize_values(authorize_values, quote)
        if "denied" in values:
            return {"status": "skipped", "reason": f"the server withheld the changed quote ({values['denied']})",
                    "submitted": False, "denied": values["denied"]}
        rearm = trace.call("planStakeResize", driver.execute_async_script, WAIT_FOR_PLAN_ARM_SCRIPT,
                           {**target, "stake": values["stake"], "timeoutMs": _plan_wait_ms(transaction), "fallbackMs": 50}) or {}
        if rearm.get("status") != "armed" or rearm.get("placeDisabled"):
            raise BetNotSubmitted("the changed stake did not re-arm")
        if validate is not None and not trace.call("planResizeLiveValidation", validate):
            raise BetNotSubmitted("the live authorization expired while resizing the stake")
    if confirmed.get("status") != "submitted":
        save_slip_capture({"stage": "plan_confirmation", "armed": armed, "confirmed": confirmed,
                           "eventId": event["eventId"], "plan": plan})
        if confirmed.get("signIn"):
            raise SignInRequired("the submit control turned into a sign-in control just before the click")
        if confirmed.get("locationBlocked"):
            raise LocationRequired(confirmed.get("reason", "location verification required"))
        if confirmed.get("edgeRejected"):
            raise BetNotSubmitted(confirmed.get("reason", "the final price failed the plan floor"),
                                  {"slipDecimal": confirmed.get("decimalOdds"),
                                   "minDecimal": confirmed.get("minDecimal"), "odds": confirmed.get("odds")})
        if confirmed.get("uiRejected") or confirmed.get("planRejected") or confirmed.get("quoteRejected"):
            raise BetNotSubmitted(confirmed.get("reason", "the final slip is not usable"),
                                  not_ready=confirmed.get("notReady") is True,window_expired=confirmed.get('windowExpired') is True)
        raise BetSlipError(confirmed.get("reason", "place-bet click failed"))
    trace.note_submission(confirmed)
    state = {"stake": values["stake"], "minDecimal": values["min_decimal"], "echo": values.get("echo")}

    def continue_plan(step):
        # Only an explicit prompt for the same claimed quote may continue this wager.
        if validate is not None and not trace.call("planConfirmationLiveValidation", validate):
            return {"status": "aborted", "reason": "the live authorization expired during confirmation"}
        provider_limit = None
        if step["kind"] == "stake-limit":
            return {"status": "aborted", "reason": "unsupported unscoped stake-limit confirmation"}
        else:
            try:
                wait_ms = _plan_wait_ms(transaction)
            except BetNotSubmitted as error:
                return {"status": "aborted", "reason": str(error)}
            quote = trace.call("planContinuationQuote", driver.execute_async_script, WAIT_FOR_PLAN_ARM_SCRIPT,
                               {**target, "readOnly": True, "continuation": step,
                                "timeoutMs": wait_ms, "fallbackMs": 50}) or {}
            if quote.get("status") != "armed":
                return {"status": "aborted", "reason": quote.get("reason", "the changed-price quote did not re-read")}
            fresh = _authorize_values(authorize_values, quote)
            if "denied" in fresh:
                return {"status": "aborted", "reason": f"the server withheld the changed price ({fresh['denied']})"}
            if fresh["stake"] != state["stake"]:
                return {"status": "aborted", "reason": "continuation stake changed; no rearming after submission"}
            state.update(stake=fresh["stake"], minDecimal=fresh["min_decimal"], echo=fresh.get("echo"))
        response = trace.call("planConfirmationFollowup", driver.execute_script, CONFIRM_PLAN_SCRIPT,
                             {**target, "stake": state["stake"], "minDecimal": state["minDecimal"], "echo": state["echo"],
                              "selectionLabel": outcome["label"], "marketName": market["name"],
                              "selectionLine": (plan.get("specification") or {}).get("line"),
                              "continuation": step, "providerStakeLimit": provider_limit}) or {}
        if response.get("status") == "submitted":
            state.update(stake=response.get("stake", state["stake"]),
                         minDecimal=response.get("minDecimal", state["minDecimal"]))
        return response

    receipt = trace.call("planReceiptWait", settle_submission, driver, confirmed, BET_RECEIPT_SCRIPT, continue_plan)
    confirmed = receipt["lastConfirmation"]
    save_slip_capture({"stage": "plan_receipt", "armed": armed, "confirmed": confirmed, "receipt": receipt,
                       "eventId": event["eventId"], "plan": plan, "timing": trace.snapshot()})
    placed = bool(receipt.get("placed"))
    if placed:
        driver._tt_plan_uncertain = False
        transaction.update(uncertain=False, touched=False)
    location_blocked = receipt.get("locationBlocked")
    return {"status": "placed" if placed else "unconfirmed", "stake": state["stake"],
            'conservativeEV':values.get('conservative_ev'),
            "sizingMode": "server_plan", "selectionId": outcome["selectionId"],
            "label": outcome["label"], "market": market["name"], "eventId": event["eventId"],
            "nextSet": plan["nextSet"], "betType": plan["betType"], "scoreState": plan["scoreState"],
            "odds": confirmed.get("odds", armed.get("odds")),
            "acceptedDecimal": confirmed.get("decimalOdds", armed.get("decimalOdds")),
            "minDecimal": state["minDecimal"], "echo": state["echo"], "submitted": True,
            "submitKind": confirmed.get("submitKind", "place-bet"),
            "submissionActions": receipt.get("actions"),
            "confirmationBlocked": receipt.get("confirmationBlocked"),
            "receipt": receipt.get("text"), "receiptPlaced": placed,
            "comparison": {"slipDecimal": confirmed.get("decimalOdds", armed.get("decimalOdds")),
                           "estimatedNetEVPercent": None}}
