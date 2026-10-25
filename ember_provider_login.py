"""Local provider login gate: no credentials, market navigation, or wager actions."""
import time

from selenium.common.exceptions import InvalidSessionIdException, NoSuchWindowException, WebDriverException

from draftkings_login import LOGIN_URL, SPORTSBOOK_URL
from driver_lifecycle import DriverConnectionLost


PROVIDER_STATE_SCRIPT = r"""
const cfg=arguments[0];
const providerPage=location.hostname===new URL(cfg.origin).hostname||location.hostname.endsWith('.draftkings.com');
const visible=e=>{if(!e?.getClientRects().length)return false;for(let p=e;p;p=p.parentElement){
    const s=getComputedStyle(p);if(s.display==='none'||s.visibility==='hidden'||Number(s.opacity)===0)return false;}return true;};
const text=e=>(e.innerText||e.textContent||'').replace(/\s+/g,' ').trim();
const form=providerPage&&[...document.querySelectorAll('#login-username-input,#login-password-input,input[type="password"]')].some(visible);
const verification=providerPage&&[...document.querySelectorAll('input[autocomplete="one-time-code"],input[id^="otc-input-"],[data-testid="otp-options-container"],[data-testid="verify-phone-number-screen"],iframe[title*="challenge" i]')].some(visible);
const login=[...document.querySelectorAll('button,a,[role="button"],span,div')].filter(visible)
    .some(e=>/^(?:sign\s*up\s*(?:or|\/)\s*)?(?:log\s*in|sign\s*in)$/i.test(text(e)));
const origin=location.origin===cfg.origin;
const signedIn=origin&&window.__isLoggedIn===true&&!form&&!verification&&!login;
const native=window.__playSpan?.playSpan;
return {on_sportsbook:origin,provider_page:providerPage,document_ready:document.readyState==='complete',
    signed_in:signedIn,explicit_signed_out:origin&&(window.__isLoggedIn===false||login),
    login_form_visible:form,verification_pending:verification,
    auth_route:providerPage&&/\/(?:login|auth|verify|verification|challenge|two-factor|2fa|otp)(?:\/|$)/i.test(location.pathname),
    provider_can_bet:origin&&window.__canBet===true,
    native_wagering_grant:origin&&!!(native?.clientKey&&native.grantType==='PlaySportsbook')};
"""

OPEN_LOGIN_SCRIPT = r"""
const cfg=arguments[0];
if(location.origin!==cfg.origin)return {status:'waiting'};
const visible=e=>{if(!e?.getClientRects().length)return false;for(let p=e;p;p=p.parentElement){
    const s=getComputedStyle(p);if(s.display==='none'||s.visibility==='hidden'||Number(s.opacity)===0)return false;}return true;};
if([...document.querySelectorAll('#login-username-input,input[type="password"],input[autocomplete="one-time-code"],input[id^="otc-input-"]')].some(visible))
    return {status:'already-open'};
const matches=[...document.querySelectorAll('button,a,[role="button"],span,div')].filter(visible)
    .filter(e=>/^(?:sign\s*up\s*(?:or|\/)\s*)?(?:log\s*in|sign\s*in)$/i.test((e.innerText||e.textContent||'').trim().replace(/\s+/g,' ')))
    .filter(e=>!e.closest('[data-testid="betslip-content-wrapper"]'));
matches.sort((a,b)=>a.getBoundingClientRect().width*a.getBoundingClientRect().height-b.getBoundingClientRect().width*b.getBoundingClientRect().height);
return matches.length?{status:'ready',button:matches[0]}:{status:'missing'};
"""


def provider_state(driver):
    """Prefer the authenticated sportsbook tab; never return credentials or grant values."""
    handles = driver.window_handles
    if isinstance(handles, (list, tuple)) and not handles:
        raise NoSuchWindowException("No provider browser windows remain")
    handles = handles if isinstance(handles, (list, tuple)) and handles else [None]
    fallback = authenticated = pending = None
    for handle in handles:
        try:
            if handle is not None:
                driver.switch_to.window(handle)
            state = driver.execute_script(PROVIDER_STATE_SCRIPT, {"origin": SPORTSBOOK_URL.rstrip("/")}) or {}
            if not isinstance(state, dict):
                continue
            if state.get("login_form_visible") or state.get("verification_pending"):
                pending = (handle, state)
            elif state.get("signed_in"):
                authenticated = (handle, state)
            if fallback is None or state.get("on_sportsbook"):
                fallback = (handle, state)
        except (InvalidSessionIdException, DriverConnectionLost):
            raise
        except WebDriverException as error:
            if browser_connection_lost(error):
                raise
            continue
    fallback = pending or authenticated or fallback
    if fallback is not None:
        handle, state = fallback
        if handle is not None:
            try:
                driver.switch_to.window(handle)
            except NoSuchWindowException:
                return {}
        return state
    return {}


def browser_connection_lost(error):
    if isinstance(error, (InvalidSessionIdException, NoSuchWindowException, DriverConnectionLost)):
        return True
    return isinstance(error, WebDriverException) and any(text in str(error).lower() for text in (
        "chrome not reachable", "not connected to devtools", "disconnected from chrome",
        "target window already closed", "web view not found", "tab crashed"))


class ProviderLoginGate:
    def __init__(self, clock=time.monotonic, *, recheck=None, active=None):
        self.clock = clock
        self.recheck = recheck
        self.active = active if active is not None else lambda: True
        self.grace_until = clock()+5
        self.open_requested = False
        self.phase = None

    def navigation_started(self):
        self.grace_until = self.clock()+5

    def poll(self, driver, location_reader):
        state = provider_state(driver)
        location = location_reader(driver)
        recheck_started = False
        if self.recheck is not None and state.get('signed_in') and location.get('blocked') and self.active():
            recheck_started = self.recheck.start(driver, location, validate=self.active)
            if recheck_started:
                self.navigation_started()
        confirmed_out = bool(state.get("explicit_signed_out") and self.clock() >= self.grace_until)
        if state.get("signed_in"):
            self.open_requested = False
            ready = bool(state.get("provider_can_bet") and state.get("native_wagering_grant") and not location.get("blocked") and self.active())
            if self.recheck is not None and self.recheck.report.get('attempted'):
                ready = ready and state.get('document_ready') is True and self.clock() >= self.recheck.hold_until
                if ready:
                    self.recheck.permission_observed()
            phase = "ready" if ready else "awaiting_location"
        else:
            ready = False
            if state.get("verification_pending"):
                self.open_requested = True
                phase = "awaiting_verification"
            elif state.get("login_form_visible"):
                self.open_requested = True
                phase = "awaiting_signin"
            elif confirmed_out and not self.open_requested and state.get("document_ready") and self.active():
                # Reserve before the browser action; a lost reply never repeats
                # a click into a form where the user may already be typing.
                self.open_requested = True
                result = driver.execute_script(OPEN_LOGIN_SCRIPT, {"origin": SPORTSBOOK_URL.rstrip("/")}) or {}
                if result.get("status") == "ready":
                    try:
                        if self.active():
                            result["button"].click()
                    except (InvalidSessionIdException, NoSuchWindowException, DriverConnectionLost):
                        raise
                    except WebDriverException:
                        pass  # Leave the browser available for ordinary user sign-in.
                elif result.get("status") == "missing" and not state.get("auth_route") and self.active():
                    driver.get(LOGIN_URL)
                phase = "awaiting_signin"
            else:
                phase = "awaiting_signin" if self.open_requested else "awaiting_provider"
        messages = {
            "awaiting_provider": "Waiting for the DraftKings sign-in page to finish loading.",
            "awaiting_signin": "DraftKings login required: enter your credentials in the local browser. Table Tennis and wager execution will wait.",
            "awaiting_verification": "Complete DraftKings SMS/CAPTCHA verification in the local browser; Ember is waiting.",
            "awaiting_location": "DraftKings sign-in confirmed. Complete its location verification; waiting for wagering permission.",
            "ready": "DraftKings sign-in and wagering permission confirmed.",
        }
        message = ('DraftKings location recheck opened; wagering remains paused until native permission returns.'
                   if recheck_started else messages[phase] if phase != self.phase else None)
        self.phase = phase
        return {**state, "ready": ready, "phase": phase, "message": message,
                "location": location, "signed_out_confirmed": confirmed_out}
