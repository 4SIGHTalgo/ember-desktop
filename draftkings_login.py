"""DraftKings navigation and local permissions for the owned browser.

The client never drives a sign-in form: the customer signs in inside the owned
browser profile. This module only navigates to the league pages, grants the
local permissions the wagering location check needs, and can sound an audible
alert when a run stops.
"""
import os
import sys
import time
from urllib.parse import urlparse

from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

LOGIN_URL = "https://sportsbook.draftkings.com/login"
SPORTSBOOK_URL = "https://sportsbook.draftkings.com/"
LOCATION_PERMISSION_ORIGINS = (
    "https://sportsbook.draftkings.com",
    "https://myaccount.draftkings.com",
    "https://www.draftkings.com",
    "https://accounts.draftkings.com",
    "https://sportsbook-nash.draftkings.com",
)
LOCATION_PERMISSION_NAMES = ("geolocation", "local-network-access", "local-network", "loopback-network")
WAIT = 25

TABLE_TENNIS_URL = "https://sportsbook.draftkings.com/sports/tabletennis"
TT_ELITE_URL = "https://sportsbook.draftkings.com/leagues/tabletennis/tt-elite-series"
TABLE_TENNIS_SELECTORS = [(By.XPATH, "//h1[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                                    "'abcdefghijklmnopqrstuvwxyz'), 'table tennis')]")]


def alert_failure():
    """An overnight run that stops should be audible, not just visible."""
    if os.name != "nt" or sys.stdout.isatty() is not True:
        return
    try:
        import winsound
        for _ in range(3):
            winsound.Beep(880, 250)
    except Exception:
        pass


def find_first(driver, selectors, timeout=WAIT):
    """Return the first element matching any selector, or None."""
    deadline = time.monotonic() + timeout
    while True:
        for by, value in selectors:
            try:
                for el in driver.find_elements(by, value):
                    if el.is_displayed() and el.is_enabled() and not el.get_property("readOnly"):
                        return el
            except WebDriverException:
                continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(0.5, remaining))


def allow_location_permissions(driver):
    """Allow real location and the local GeoComply connection for DraftKings."""
    info = driver.execute_cdp_cmd("Target.getTargetInfo", {})
    context = info.get("targetInfo", {}).get("browserContextId") if isinstance(info, dict) else None
    # grantPermissions grants its list AND denies every unlisted permission.
    # Chrome 155 separates local-network from the older local-network-access;
    # the old allowlist silently denied the very prompt needed by the helper.
    # Set only these individual permissions and leave all others unchanged.
    # shortcut: Chrome 152/macOS rejects Target.getTargetInfo's browserContextId
    # ("Failed to find browser context for id"); our owned page lives in the
    # default context, so an unresolvable id falls back to it, once per run.
    unresolved = []
    def grant(permission):
        if context and not unresolved:
            permission = {**permission, "browserContextId": context}
        try:
            driver.execute_cdp_cmd("Browser.setPermission", permission)
        except WebDriverException as error:
            if (not context or unresolved
                    or "Failed to find browser context" not in str(getattr(error, "msg", error))):
                raise
            unresolved.append(True)
            driver.execute_cdp_cmd("Browser.setPermission",
                {key: value for key, value in permission.items() if key != "browserContextId"})
    for origin in LOCATION_PERMISSION_ORIGINS:
        for name in LOCATION_PERMISSION_NAMES:
            grant({"origin": origin, "permission": {"name": name}, "setting": "granted"})


def goto_table_tennis(driver):
    print("Navigating to Table Tennis...")
    driver.get(TABLE_TENNIS_URL)
    try:
        WebDriverWait(driver, WAIT).until(
            lambda d: urlparse(d.current_url).hostname == urlparse(TABLE_TENNIS_URL).hostname
            and urlparse(d.current_url).path.rstrip('/') == urlparse(TABLE_TENNIS_URL).path.rstrip('/')
            and find_first(d, TABLE_TENNIS_SELECTORS, timeout=0) is not None
        )
    except TimeoutException:
        raise RuntimeError("Could not confirm the Table Tennis page. Check for a login or location prompt.") from None
    print("Table Tennis page confirmed.")


def has_saved_session(driver, timeout=None):
    """Read-only positive confirmation; never interrupt a challenge or reload login."""
    from tt_market_exec import signed_in_state
    def account_state(d):
        handles = d.window_handles
        handles = handles if isinstance(handles, (list, tuple)) and handles else [None]
        for handle in handles:
            try:
                if handle is not None:
                    d.switch_to.window(handle)
                if (urlparse(d.current_url).hostname == urlparse(SPORTSBOOK_URL).hostname
                        and signed_in_state(d) == 'signed-in'):
                    return True
            except WebDriverException:
                continue
        return False
    try:
        return bool(WebDriverWait(driver, WAIT if timeout is None else timeout, poll_frequency=.1).until(account_state))
    except TimeoutException:
        return False


def goto_tt_elite(driver):
    """Use the match-list league as standby after provider authentication."""
    driver.get(TT_ELITE_URL)
    selectors = [(By.XPATH, "//*[self::h1 or self::h2][contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ-', 'abcdefghijklmnopqrstuvwxyz '), 'tt elite')]"),
                 (By.CSS_SELECTOR, 'a[href*="/event/"], button[data-testid^="component-builder-market-button-"]')]
    try:
        WebDriverWait(driver, WAIT, poll_frequency=.1).until(lambda d:
            urlparse(d.current_url).hostname == urlparse(TT_ELITE_URL).hostname
            and urlparse(d.current_url).path.rstrip("/") == urlparse(TT_ELITE_URL).path.rstrip("/")
            and find_first(d, selectors, timeout=0) is not None)
    except TimeoutException:
        raise RuntimeError("Could not confirm the TT Elite league match list. Check the local browser's login, location, or load notice.") from None
