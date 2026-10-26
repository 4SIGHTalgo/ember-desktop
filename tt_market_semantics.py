"""Shared market semantics: verified score identity and odds reading.

Both sides of the wire need these observable-fact rules: the scanner builds
contexts on them and the public executor validates signals, verifies close-out
states, and reads slip prices with them. They carry no strategy — no
probabilities, no EV, no sizing.

Layer: protocol semantics (stdlib plus draftkings_live only).
"""
from functools import lru_cache
import math
import re

import draftkings_live as dk
from ember_identity import name_key


def pair_key(match):
    return tuple(sorted((name_key(match["player1"]), name_key(match["player2"]))))


def finished(event):
    tally = event.get("setTally") or []
    return (event.get("status") in {"FINISHED", "ENDED", "FINAL", "COMPLETED", "CANCELLED", "ABANDONED"}
            or len(tally) == 2 and all(isinstance(v, int) for v in tally) and max(tally) >= 3)


def completed_scores(scores):
    """Ordered completed-set prefix; None means malformed or missing set numbers."""
    if not isinstance(scores, (list, dict)):
        return None
    rows = []
    for number, score in scores.items() if isinstance(scores, dict) else enumerate(scores):
        if score is None:
            continue
        if not isinstance(score, dict):
            return None
        number = score.get("set", number)
        if not str(number).isascii() or not str(number).isdigit() or not 1 <= int(number) <= 5:
            return None
        home, away = dk.point(str(score.get("home"))), dk.point(str(score.get("away")))
        if home is None or away is None:
            return None
        rows.append((int(number), home, away))
    prefix = []
    for number, home, away in sorted(rows):
        if number != len(prefix) + 1:
            return None
        if max(home, away) < 11 or abs(home - away) < 2:
            break
        prefix.append((home, away))
    return prefix


def next_set_number(event):
    if not event or event.get("status") != "STARTED" or finished(event):
        return None
    scores = completed_scores(event.get("sets"))
    period = re.fullmatch(r"([1-5])(?:st|nd|rd|th)", event.get("period") or "")
    if (scores is None or not period or len(scores) >= 5 or int(period[1]) > len(scores) + 1
            or any(sum((h > a) == side for h, a in scores) >= 3 for side in (False, True))):
        return None
    return len(scores) + 1


def score_state(event):
    """Verified completed-set tally, never an inferred or terminal score."""
    if not event or next_set_number(event) is None:
        return None
    scores = completed_scores(event.get("sets"))
    if scores is None:
        return None
    state = [sum(h > a for h, a in scores), sum(a > h for h, a in scores)]
    return state if state == event.get("setTally") else None


def closeout_state(event):
    state = score_state(event)
    return state if state and max(state) == 2 and min(state) in (0, 1, 2) else None


def entry_window(event):
    """Historical start-state probabilities apply before the next set's first point."""
    state = score_state(event)
    if state is None:
        return False
    target = sum(state) + 1
    if target in event.get("entryClosedGames", []):
        return False
    rows = event.get("sets") or []
    rows = rows.values() if isinstance(rows, dict) else rows
    next_score = next((s for s in rows if isinstance(s, dict) and s.get("set") == target), None)
    current = event.get("currentSet") or {}
    if current.get("set") == target:
        if current.get("home") != 0 or current.get("away") != 0:
            return False
        next_score = next_score or current
    if next_score is not None:
        return next_score.get("home") == 0 and next_score.get("away") == 0
    period = re.fullmatch(r"([1-5])(?:st|nd|rd|th)", event.get("period") or "")
    return bool(period and int(period[1]) == target - 1)


def implied_probability(decimal):
    return (1 / decimal if type(decimal) in (int, float) and math.isfinite(decimal) and decimal > 1
            else None)


def price_text(text):
    """Slip prices made printable: DraftKings uses the typographic minus sign."""
    return str(text or "").strip().replace("−", "-").replace("–", "-")


def american_odds(text):
    """Decimal odds for a slip price such as '-140' or '+101'; None when unreadable."""
    cleaned = price_text(text)
    if not re.fullmatch(r"[+-]?\d+", cleaned):
        return None
    value = int(cleaned)
    if value == 0:
        return None
    return 1 + (100 / -value if value < 0 else value / 100)


def outcome_decimal(outcome):
    """Prefer the displayed American quote to the feed's rounded decimal field."""
    american = american_odds((outcome or {}).get("american"))
    return american if american is not None else (outcome or {}).get("decimal")


def context_matches_market(info, market):
    from execution_policy import MARKET_FAMILIES as MARKETS
    if info.get("betType") in MARKETS.values():
        return MARKETS.get(market.get("name")) == info["betType"]
    if info.get("betType") == "closeout":
        return market.get("name") == "Moneyline"
    return (market.get("name") == f"Game {info['nextSet']}"
            or info["nextSet"] == 5 and market.get("name") == "Moneyline")


def bet_selection(info, market):
    """Resolve the actual quoted side; economic eligibility is evaluated separately."""
    if not info or info.get("nextSet") not in (2, 3, 4, 5) or not info.get("conditioningCount") or not context_matches_market(info, market):
        return None
    loser = name_key(info.get("selection") or info["previousLoser"])
    matches = [outcome for outcome in market["outcomes"] if name_key(outcome["label"]) == loser]
    return matches[0] if len(matches) == 1 else None


@lru_cache(maxsize=128)
def terminal_sequences(sequence):
    if max(sequence.count(0), sequence.count(1)) == 3:
        return (sequence,)
    return terminal_sequences(sequence + (0,)) + terminal_sequences(sequence + (1,))


def payoff_key(context, prefix):
    """Canonical binary partition, merging both equal and opposite directions."""
    spec = context.get("specification")
    family = context.get("betType")
    if spec is None:
        return None
    sequence = tuple(int(h < a) for h, a in prefix)
    family = spec["family"]
    if family == "point_handicap" and sequence.count(0) == sequence.count(1) == 2:
        diff = sum(row[spec["player"]] - row[1-spec["player"]] for row in prefix) + spec["line"]
        if -2 < diff < 2:
            spec = {"family": "closeout", "player": spec["player"]}
            family = "closeout"
    if family in {"split", "complement", "closeout", "set_handicap", "correct_score"}:
        values = []
        for final in terminal_sequences(sequence):
            counts = [final.count(0), final.count(1)]
            if family in {"split", "complement"}:
                value = final[spec["target"] - 1] == spec["player"]
            elif family == "closeout":
                value = counts[spec["player"]] == 3
            elif family == "correct_score":
                value = counts == list(spec["score"])
            else:
                value = counts[spec["player"]] - counts[1-spec["player"]] + spec["line"] > 0
            values.append(value)
        if len(set(values)) < 2:
            return ("determined",)
        return ("winner-event", sequence, min(tuple(values), tuple(not value for value in values)))
    return (family,)
