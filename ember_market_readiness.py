"""Non-wager market warmup and deadline-bounded recovery of transient UI states."""
import time

from tt_market_exec import LOAD_RECOVERY_SCRIPT, plan_slip_ready, read_plan_slip, remove_closed_picks
from driver_lifecycle import connection_lost


PROVIDER_HOST = "sportsbook.draftkings.com"
WARM_MARKET_SCRIPT = r"""
const cfg=arguments[0];
const normalize=text=>String(text||'').toLowerCase().replace(/[łøđ]/g,c=>({ł:'l',ø:'o',đ:'d'})[c])
    .normalize('NFKD').replace(/\p{M}/gu,'').replace(/[^\p{L}\p{N}]+/gu,' ').trim();
const visible=e=>{if(!e?.getClientRects().length)return false;for(let p=e;p instanceof Element;p=p.parentElement){
    const s=getComputedStyle(p);if(s.display==='none'||s.visibility==='hidden'||Number(s.opacity)===0)return false;}return true;};
if(location.hostname!==cfg.host||location.pathname.replace(/\/$/,'').split('/').pop()!==cfg.eventId)
    return {status:'waiting',reason:'event route not ready'};
const boards=[...document.querySelectorAll('.cms-event-scoreboard-container')].filter(visible);
if(!boards.some(board=>cfg.players.every(player=>` ${normalize(board.innerText)} `.includes(` ${normalize(player)} `))))
    return {status:'waiting',reason:'scoreboard identity'};
const tabs=[...document.querySelectorAll('[role="tab"][aria-selected="true"]')].filter(visible);
const selected=tabs.map(tab=>(tab.textContent||'').trim());
if(!selected.some(label=>cfg.tabs.includes(label)))return {status:'waiting',reason:'market tab not selected'};
const titles=cfg.sections.map(normalize);
const groups=[...document.querySelectorAll('[data-testid="collapsible-wrapper"]')].filter(group=>visible(group)&&
    titles.includes(normalize(group.querySelector('[data-testid="collapsible-header"]')?.textContent)));
const kind=cfg.distribution?'market':'winner';
if(!groups.length)return {status:'waiting',reason:kind+' section missing'};
if(groups.length!==1)return {status:'waiting',reason:kind+' section ambiguous'};
const group=groups[0],trigger=group.querySelector('[data-testid="collapsible-trigger"]');
let state=window.__emberMarketWarm;
if(!state||state.key!==cfg.key||state.group!==group)state=window.__emberMarketWarm={key:cfg.key,group,positioned:false,trigger:null,lastClick:0};
if(!state.positioned){state.positioned=true;group.scrollIntoView({block:'center',behavior:'instant'});}
if(trigger?.getAttribute('aria-expanded')==='false'){
    if(state.trigger!==trigger||performance.now()-state.lastClick>1000){state.trigger=trigger;state.lastClick=performance.now();trigger.click();}
    return {status:'waiting',reason:'expanding '+kind+' section'};
}
state.trigger=null;
return {status:'ready',reason:null};
"""


class MarketReadiness:
    def __init__(self, core, clock=time.monotonic, sleep=time.sleep):
        self.core, self.clock, self.sleep = core, clock, sleep
        self.retry_at = 0
        self.load_errors = 0
        self.error_reason = 'provider load error; preparation paused'

    def provider_ready(self, driver):
        if self.clock() < self.retry_at:
            return False
        state = driver.execute_script(LOAD_RECOVERY_SCRIPT, {})
        if not isinstance(state, dict) or state.get('present') is not False:
            self.retry_at = self.clock()+5
            self.load_errors += 1
            if self.load_errors == 3:
                self.error_reason = 'provider load error; awaiting a positively verified recovery'
                driver.execute_script(self.core.REVEAL_SCRIPT, None)
            return False
        self.load_errors = 0
        self.error_reason = 'provider load error; preparation paused'
        return True

    def prepare(self, driver, event, market, valid, remaining, plan=None, max_wait=.3, staging=False,observe_price=None):
        """One shared FIFO turn; an exact ready slip leg needs no board at all."""
        deadline = self.clock()+max_wait
        if not self.provider_ready(driver):
            return {'status': 'not_ready', 'reason': self.error_reason, 'attempts': 0, 'wait_ms': 0}
        if observe_price is not None and plan is not None:
            for leg in read_plan_slip(driver).get('legs',[]):
                if (all(leg.get(key)==value for key,value in {'eventId':event['eventId'],'marketId':market['marketId'],
                        'selectionId':plan['selectionId']}.items()) and type(leg.get('decimalOdds')) in (int,float)
                        and not observe_price(leg['decimalOdds'])):
                    return {'status':'not_ready','reason':'conservative EV decreased','windowExpired':True,'attempts':0,'wait_ms':0}
        if plan is not None and valid() and plan_slip_ready(driver, event, market, plan):
            return {"status": "revealed", "staged": True, "attempts": 0, "wait_ms": 0}
        if plan is not None:
            snapshot = read_plan_slip(driver)
            if (snapshot.get('stagingSafe') is True and valid() and remaining() > .6
                    and any(leg.get('closed') is True for leg in snapshot.get('legs', []))):
                removed = remove_closed_picks(driver)
                if removed.get('status') != 'cleared':
                    return {'status':'not_ready','reason':'closed picks could not be removed','attempts':0,'wait_ms':0}
                if valid() and plan_slip_ready(driver, event, market, plan):
                    return {'status':'revealed','staged':True,'attempts':0,'wait_ms':0}
                snapshot = read_plan_slip(driver)
            if snapshot.get('status') not in ('empty','present') or snapshot.get('stagingSafe') is not True:
                return {'status':'not_ready','reason':snapshot.get('reason') or 'slip funding is not zero',
                        'attempts':0,'wait_ms':0}
            legs = snapshot.get('legs', [])
            if plan.get('window') and any(leg.get('eventId')==event['eventId'] and leg.get('marketId')==market['marketId']
                    and leg.get('selectionId')==plan['selectionId'] and type(leg.get('decimalOdds')) in (int,float)
                    and leg['decimalOdds']<plan['min_decimal'] for leg in legs):
                return {'status':'not_ready','reason':'conservative EV decreased','windowExpired':True,'attempts':0,'wait_ms':0}
            if isinstance(legs, list) and any(leg.get('eventId') == event['eventId'] and
                    leg.get('marketId') == market['marketId'] and leg.get('selectionId') == plan['selectionId']
                    and leg.get('suspended') is True for leg in legs):
                return {'status': 'not_ready', 'reason': 'target leg suspended', 'attempts': 0, 'wait_ms': 0}
        if not hasattr(self.core, 'correct_event') or not self.core.correct_event(driver, event):
            if staging:
                return {'status': 'not_ready', 'reason': 'event identity not ready', 'attempts': 0, 'wait_ms': 0}
            # A signal turn may still have to cross matches; give that one
            # navigation a real budget instead of a yield-sized slice.
            nav_wait = min(2.0, max(.3, max(0, remaining()-.6))) if plan is not None else max(0, deadline-self.clock())
            opening = self.open(driver, event, market, valid, remaining, max_wait=nav_wait)
            if opening.get("status") != "opened":
                return opening
            if plan is not None:
                # Navigation has its own budget; a verified new event still needs a bounded reveal turn.
                deadline=self.clock()+max_wait
        if plan is not None:
            market = {**market, 'outcomes': [row for row in market.get('outcomes', [])
                                           if row.get('selectionId') == plan['selectionId']]}
        return self.wait(driver, event, market, valid, remaining,
                         max_wait=max(0, deadline-self.clock()), staging=staging)

    def fallback_tab(self, driver, event, market):
        """Leave a summary view only after the exact requested row is missing."""
        if not hasattr(self.core, "native_navigation_click") or not self.core.correct_event(driver, event):
            return False
        selected = driver.execute_script("return [...document.querySelectorAll('[role=tab][aria-selected=true]')].map(e=>(e.innerText||e.textContent||'').trim())")
        if not isinstance(selected, list):
            return False
        from execution_policy import MARKET_FAMILIES as MARKETS
        closeout = market.get("name") == "Moneyline" or market.get("betType") == "closeout" or market.get('name') in MARKETS
        if not any(label in (["Popular"] if closeout else ["Popular", "Match Lines"]) for label in selected):
            return False
        for label in (["Match Lines"] if closeout else ["Game Props", "Player Props"]):
            for tab in driver.find_elements(self.core.By.XPATH, f"//*[@role='tab' and normalize-space(.)='{label}']"):
                if tab.is_displayed() and tab.text.strip() == label:
                    return self.core.native_navigation_click(driver, tab, label)
        return False

    def warm(self, driver, event, hint):
        if not self.provider_ready(driver):
            return {'status': 'waiting', 'reason': self.error_reason}
        family = hint.get("betType", "split")
        surface = self.core.market_surface(hint)
        if not self.core.correct_event(driver, event):
            return {"status": "waiting", "reason": "event identity not ready"}
        if not self.core.ensure_market_tab(driver, event, hint, timeout=.1, verified=True):
            return {"status": "waiting", "reason": "market tab not selected"}
        reading = driver.execute_script(WARM_MARKET_SCRIPT, {
            "host": PROVIDER_HOST, "eventId": event["eventId"], "players": [event["player1"], event["player2"]],
            "key": event["eventId"]+":"+family+":"+surface['sectionTitle'],
            "sections": surface['sectionTitles'], "tabs": surface['tabLabels'],
            "distribution": surface['distribution']})
        return reading if isinstance(reading, dict) else {"status": "waiting", "reason": "market surface not ready"}

    def open(self, driver, event, market, valid, remaining, reserve=.6, max_wait=None):
        if not self.provider_ready(driver):
            return {'status': 'not_ready', 'reason': self.error_reason, 'attempts': 0, 'wait_ms': 0}
        started = self.clock()
        deadline = started+max(0, remaining()-reserve)
        if max_wait is not None:
            deadline = min(deadline, started+max_wait)
        attempts = 0
        while valid():
            left = min(deadline-self.clock(), remaining()-reserve)
            if left <= 0:
                break
            attempts += 1
            try:
                if self.core.open_props(driver, event, market, timeout=min(.4, left)):
                    return {"status": "opened", "attempts": attempts, "wait_ms": round(1000*(self.clock()-started), 1)}
            except self.core.FATAL_BROWSER_ERRORS:
                raise
            except self.core.BrowserNavigationPending:
                pass
            left = min(deadline-self.clock(), remaining()-reserve)
            if left <= 0:
                break
            self.sleep(min(.03, left))
        return {"status": "expired" if not valid() else "not_ready", "reason": "event identity not ready",
                "attempts": attempts, "wait_ms": round(1000*(self.clock()-started), 1)}

    def wait(self, driver, event, market, valid, remaining, reserve=.6, max_wait=None, staging=False):
        """Retry only pre-submit reveal/expansion; never retry a wager command."""
        started = self.clock()
        deadline = started+max(0, remaining()-reserve)
        if max_wait is not None:
            deadline = min(deadline, started+max_wait)
        attempts = 0
        fallback_attempted = False
        last = {"status": "waiting", "reason": "market surface not ready"}
        while valid():
            if self.clock() >= deadline or remaining() <= reserve:
                break
            attempts += 1
            try:
                reading = (self.core.reveal_set_winner(driver, event, market, staging=True)
                           if staging and market.get('name') == 'Moneyline' else
                           self.core.reveal_set_winner(driver, event, market))
                last = reading if isinstance(reading, dict) else {"status": "waiting", "reason": "market surface not ready"}
            except self.core.FATAL_BROWSER_ERRORS:
                raise
            except self.core.WebDriverException as error:
                if connection_lost(error):
                    raise
                last = {"status": "waiting", "reason": "market surface not ready"}
            if last.get("status") == "revealed" and valid():
                return {**last, "attempts": attempts, "wait_ms": round(1000*(self.clock()-started), 1)}
            if (not fallback_attempted and self.clock() < deadline and remaining() > reserve
                    and last.get("reason") in {"winner section missing", "market section missing", "selection IDs missing or ambiguous"}):
                try:
                    fallback_attempted = self.fallback_tab(driver, event, market)
                except self.core.FATAL_BROWSER_ERRORS:
                    raise
                except self.core.WebDriverException:
                    pass
            left = min(deadline-self.clock(), remaining()-reserve)
            if left <= 0:
                break
            self.sleep(min(.03, left))
        if max_wait is None or not valid() or remaining() <= reserve:
            try:
                driver.execute_script(self.core.REVEAL_SCRIPT, None)
            except self.core.FATAL_BROWSER_ERRORS:
                raise
            except self.core.WebDriverException:
                pass
        return {"status": "expired" if not valid() else "not_ready", "reason": last.get("reason") or "market surface not ready",
                "attempts": attempts, "wait_ms": round(1000*(self.clock()-started), 1)}
