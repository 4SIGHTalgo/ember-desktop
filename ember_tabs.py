"""Execution layer: keep one pre-loaded DraftKings tab per live match.

A cross-match signal re-routing the single page waited 3 s+ for DraftKings to
render the other event (live, 2026-10-27), most of a 5 s signal. Switching to an
already-rendered tab takes milliseconds. Tabs hold no execution state: every turn
still verifies the event identity, the market and the single slip in the tab it
acts in.
"""
import re
import time
from datetime import datetime
from urllib.parse import urlsplit

# 1 = route in place, never open a tab. Live 2026-10-28: a freshly opened DK event tab spent 20-30 s
# of main thread hydrating (each command into it hung to the 25 s timeout, three recoveries in a row),
# and a background tab woke with a 10-12 s stall; in-place routing costs ~3 s per cross-match turn.
MAX_TABS = 1
RETIRE_GRACE = 60  # seconds a match may be off the live board (feed reconnects) before its tab closes
# The live board keeps a match's last row when DraftKings drops the event without a final score
# (live, 2026-10-27), so a row whose score has not moved this long no longer keeps its tab open.
# shortcut: a fixed age; a live match in a long medical/technical pause can lose its tab (it reopens on demand).
LIVE_SCORE_AGE = 240
EVENT_PATH = re.compile(r'/event/[^/?#]+/([0-9]{1,32})/?$')


def event_of(url):
    match = EVENT_PATH.search(urlsplit(url if isinstance(url, str) else '').path)
    return match[1] if match else None


def live_events(board, now):
    """Event ids the board still shows as live: started, not decided, and scored recently."""
    live = set()
    for row in board.get('events', []):
        tally = row.get('set_tally') or []
        scored = datetime.fromisoformat(row['score_at'].replace('Z', '+00:00')).timestamp()
        if row.get('status') == 'STARTED' and not (len(tally) == 2 and max(tally) >= 3) and now-scored <= LIVE_SCORE_AGE:
            live.add(row['event_id'])
    return live


def working(book):
    """The tab Ember last worked in, or None."""
    return max(book, key=lambda handle: book[handle].get('used', 0), default=None) if isinstance(book, dict) else None


def adopt(driver, book, clock=time.monotonic):
    """Bind the tab record to a (re)attached driver and take back match tabs a lost driver had opened.

    Only DraftKings event pages are adopted; any other window (a sign-in popup) stays unknown.
    """
    driver._ember_tabs = book
    handles = driver.window_handles
    if not isinstance(handles, list):
        return []
    for handle in [h for h in book if h not in handles]:
        book.pop(handle)
    current, adopted = driver.current_window_handle, []
    here = current
    for handle in [h for h in handles if h not in book]:
        if handle != here:
            driver.switch_to.window(handle)
            here = handle
        url = driver.current_url
        if urlsplit(url).hostname == 'sportsbook.draftkings.com' and event_of(url):
            book[handle] = {'event': event_of(url), 'used': clock()}
            adopted.append(handle)
    if here != current:
        driver.switch_to.window(current)
    return adopted


def show(driver, event, url, limit=MAX_TABS, clock=time.monotonic):
    """Bring the event's own tab forward, or open one; None leaves routing to the caller."""
    identifier = event['eventId']
    book = getattr(driver, '_ember_tabs', None)
    if not isinstance(book, dict):
        book = driver._ember_tabs = {}
    handles = driver.window_handles
    if not isinstance(handles, list):
        return None
    for handle in [h for h in book if h not in handles]:
        book.pop(handle)
    current = driver.current_window_handle
    # The current tab may have been routed elsewhere since it was recorded.
    seen = event_of(driver.current_url)
    row = book.setdefault(current, {})
    if row.get('event') != seen:
        row.clear()
    row.update(event=seen, used=clock())
    if row['event'] == identifier:
        return None
    target = next((h for h, row in book.items() if h != current and row['event'] == identifier), None)
    if target is not None:
        driver.switch_to.window(target)
        if event_of(driver.current_url) == identifier:
            driver.execute_cdp_cmd('Page.bringToFront', {})
            book[target]['used'] = clock()
            return 'switched'
        book[target]['event'] = event_of(driver.current_url)
        driver.switch_to.window(current)
    if book[current]['event'] is None or 'gone' in book[current]:
        return None  # A league, blank or finished match page is free to route; it holds no live match.
    if limit <= 1:
        return None  # In-place routing only.
    if len(handles) >= limit:
        oldest = min((h for h in book if h != current), key=lambda h: book[h]['used'], default=None)
        if oldest is None:
            return None
        driver.switch_to.window(oldest)
        driver.close()
        book.pop(oldest)
        driver.switch_to.window(current)
    driver.switch_to.new_window('tab')
    book[driver.current_window_handle] = {'event': identifier, 'used': clock()}
    driver.get(url)
    return 'opened'


def retire(driver, live, clock=time.monotonic, grace=RETIRE_GRACE):
    """Close tabs whose match is no longer live; the current tab always stays and re-routes in place.

    live is the set of live verified event ids, or None when the board is unknown (never prune then).
    """
    book = getattr(driver, '_ember_tabs', None)
    if live is None or not isinstance(book, dict) or not book:
        return []
    handles = driver.window_handles
    if not isinstance(handles, list):
        return []
    current, now, closed = driver.current_window_handle, clock(), []
    for handle, row in list(book.items()):
        if handle not in handles:
            book.pop(handle)
            continue
        if row.get('event') is None or row['event'] in live:
            row.pop('gone', None)
            continue
        row.setdefault('gone', now)
        if handle == current or now-row['gone'] < grace or len(handles)-len(closed) <= 1:
            continue
        driver.switch_to.window(handle)
        driver.close()
        book.pop(handle)
        closed.append(row['event'])
    if closed:
        driver.switch_to.window(current)
    return closed
