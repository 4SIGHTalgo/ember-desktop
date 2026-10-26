"""Data-only identity and score boundaries for a pre-set-reference execution window."""
import hashlib
import json
import re

KIND = 'pregame_non_decreasing'


def points(event, target, state):
    """Observed unfinished target set; completed prefix and orientation must agree."""
    if (event.get('status') != 'STARTED' or type(target) is not int or not 1 <= target <= 5
            or not isinstance(state, list) or len(state) != 2
            or any(type(n) is not int or not 0 <= n <= 2 for n in state)
            or sum(state)+1 != target or event.get('setTally') != state):
        return None
    rows = event.get('sets')
    if not isinstance(rows, list) or not target-1 <= len(rows) <= target:
        return None
    tally = [0, 0]
    current = None
    for number, row in enumerate(rows, 1):
        if (not isinstance(row, dict) or row.get('set') != number
                or any(type(row.get(side)) is not int or not 0 <= row[side] <= 999 for side in ('home','away'))):
            return None
        a, b = row['home'], row['away']
        if number < target:
            if not (max(a,b) == 11 and min(a,b) <= 9 or max(a,b) > 11 and abs(a-b) == 2):
                return None
            tally[int(a < b)] += 1
            if max(tally) >= 3:
                return None
        else:
            if max(a,b) >= 11 and abs(a-b) >= 2:
                return None
            current = (a,b)
    if tally != state:
        return None
    reported = event.get('currentSet')
    if reported is not None:
        if (not isinstance(reported, dict) or reported.get('set') != target
                or any(type(reported.get(side)) is not int or not 0 <= reported[side] <= 999 for side in ('home','away'))):
            return None
        pair = (reported['home'], reported['away'])
        if current is not None and pair != current or max(pair) >= 11 and abs(pair[0]-pair[1]) >= 2:
            return None
        current = pair
    period = re.fullmatch(r'([1-5])(?:st|nd|rd|th)', event.get('period') or '')
    if not period:
        return None
    if int(period[1]) == target:
        return current
    return (0,0) if int(period[1]) == target-1 and current in (None,(0,0)) else None


def identity(event, market, context,confidence=None):
    prefix = event['sets'][:context['nextSet']-1]
    value = [event['eventId'],event.get('startTimestamp'),event['player1'],event['player2'],
             market['marketId'],context['selectionId'],context['betType'],context['nextSet'],
             context['scoreState'],context['specification'],prefix,
             confidence if confidence is not None else (context.get('executionWindow') or context.get('window') or {}).get('confidence',90)]
    return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()
