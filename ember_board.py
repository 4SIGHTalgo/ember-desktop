"""Product-layer display validation; board rows never authorize a wager."""
from copy import deepcopy
from datetime import datetime
import json
import math
import re
from ember_identity import provider_id


MARKETS = {"Moneyline", "Game 2", "Game 3", "Game 4", "Game 5", "Games Spread", "Points Spread"}


def identifier(value, kind="market"):
    return provider_id(value, kind)


def stamp(value):
    if not isinstance(value, str) or len(value) > 64 or datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
        raise ValueError("Invalid board timestamp")


def numeric(value, low=-1e9, high=1e9):
    return value is None or type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def validate_board(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or value.get("protocol") != 1
            or len(json.dumps(value, allow_nan=False)) > 512000):
        raise ValueError("Invalid match board")
    stamp(value.get("updated_at")); stamp(value.get("expires_at"))
    updated, expires = (datetime.fromisoformat(value[k].replace("Z", "+00:00")) for k in ("updated_at", "expires_at"))
    if not 0 < (expires-updated).total_seconds() <= 15:
        raise ValueError("Invalid board lifetime")
    events = value.get("events")
    if not isinstance(events, list) or len(events) > 128:
        raise ValueError("Invalid board events")
    seen = set()
    for event in events:
        if not isinstance(event, dict) or not identifier(event.get("event_id"), "event") or event["event_id"] in seen:
            raise ValueError("Invalid board event identity")
        seen.add(event["event_id"])
        players = event.get("players")
        if not isinstance(players, list) or len(players) != 2 or players[0] == players[1] or any(
                not isinstance(p, str) or not 0 < len(p) <= 200 for p in players):
            raise ValueError("Invalid board participants")
        if not isinstance(event.get("status"), str) or len(event["status"]) > 40:
            raise ValueError("Invalid board status")
        if event.get("period") is not None and (not isinstance(event["period"], str) or len(event["period"]) > 40):
            raise ValueError("Invalid board period")
        for key in ("set_tally", "points"):
            pair = event.get(key)
            if pair is not None and (not isinstance(pair, list) or len(pair) != 2 or any(type(n) is not int or not 0 <= n <= 999 for n in pair)):
                raise ValueError("Invalid board score")
        stamp(event.get("score_at"))
        sets = event.get("sets")
        if not isinstance(sets, list) or len(sets) > 5:
            raise ValueError("Invalid board sets")
        for row in sets:
            if not isinstance(row, dict) or type(row.get("set")) is not int or not 1 <= row["set"] <= 5 or any(
                    type(row.get(k)) is not int or not 0 <= row[k] <= 999 for k in ("home", "away")):
                raise ValueError("Invalid board set score")
        markets = event.get("markets")
        if not isinstance(markets, list) or len(markets) > 32:
            raise ValueError("Invalid board markets")
        market_ids = set()
        for market in markets:
            if (not isinstance(market, dict) or not identifier(market.get("market_id"))
                    or market["market_id"] in market_ids or market.get("name") not in MARKETS):
                raise ValueError("Invalid board market identity")
            market_ids.add(market["market_id"])
            outcomes = market.get("outcomes")
            if not isinstance(outcomes, list) or len(outcomes) > 16:
                raise ValueError("Invalid board outcomes")
            selections = set()
            for outcome in outcomes:
                if (not isinstance(outcome, dict) or not identifier(outcome.get("selection_id"), "selection")
                        or outcome["selection_id"] in selections or outcome.get("label") not in players
                        or type(outcome.get("entry_open")) is not bool):
                    raise ValueError("Invalid board selection")
                selections.add(outcome["selection_id"])
                if not numeric(outcome.get("line"), -1e6, 1e6) or not numeric(outcome.get("decimal_odds"), 1+1e-12, 1e6):
                    raise ValueError("Invalid displayed offer")
                for key in ("conservative_ev", "entry_ev"):
                    if not numeric(outcome.get(key)):
                        raise ValueError("Invalid displayed EV")
                for key in ("lower_probabilities", "entry_evs"):
                    values = outcome.get(key, {})
                    if not isinstance(values, dict) or any(k not in {"90", "95"} or not numeric(n) for k, n in values.items()):
                        raise ValueError("Invalid confidence EVs")
                stamp(outcome.get("quote_at"))
                references=outcome.get('reference_windows',{})
                if not isinstance(references,dict) or not references.keys()<={'90','95'}:
                    raise ValueError('Invalid reference window confidence')
                for reference in [outcome.get('reference_window'),*references.values()]:
                    if reference is not None and (not isinstance(reference,dict) or set(reference)!={'kind','key','status'}
                            or reference['kind']!='pregame_non_decreasing' or not isinstance(reference['key'],str)
                            or not re.fullmatch(r'[0-9a-f]{64}',reference['key'])
                            or reference['status'] not in {'open','awaiting','paused','cancelled'}):
                        raise ValueError('Invalid reference window display')
    return value


def display_board(scanner, level):
    if not scanner or not scanner.get("board"):
        return scanner
    result = deepcopy(scanner)
    confidence = "95" if level == "very_conservative" else "90"
    for event in result["board"]["events"]:
        for market in event["markets"]:
            for outcome in market["outcomes"]:
                outcome["conservative_ev"] = (outcome.get("lower_probabilities") or {}).get(confidence)
                outcome["entry_ev"] = (outcome.get("entry_evs") or {}).get(confidence)
                if 'reference_windows' in outcome:
                    outcome['reference_window']=outcome['reference_windows'].get(confidence)
                    outcome['entry_open']=bool(outcome['reference_window'] and outcome['reference_window']['status']=='open'
                        and outcome['conservative_ev'] is not None and outcome['conservative_ev']>1e-9)
    return result
