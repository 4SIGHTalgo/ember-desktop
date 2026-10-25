"""Bounded, display-only snapshots of the scanner's existing strategy decisions."""
from datetime import datetime, timedelta, timezone
import math


FIELDS = {"protocol", "event_id", "players", "set_tally", "points", "period", "market", "bet_type",
          "reason", "mapping_verified", "entry_open", "qualifies", "minimum_ev", "ev_mode", "confidence",
          "history_count", "candidates", "updated_at", "expires_at", "score_at", "market_at"}
CANDIDATE_FIELDS = {"player", "decimal_odds", "american_odds", "probability", "lower_probability",
                    "implied_probability", "estimated_ev", "conservative_ev", "gate_ev",
                    "sample_size", "success_count", "ev_passed", "line"}


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def stamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Invalid opportunity timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Opportunity timestamps require a timezone")
    return parsed


def history_display_reports(reports, mapped, matches, now, source_fresh):
    """Annotate telemetry without changing the scanner's fits or wager decisions.

    Keep the v1 display schema compatible with installed clients: history_count
    means known clean prior meetings; the reason describes a smaller frozen fit.
    """
    histories = {match["id"]: match for match in matches}
    mappings = {(row.get("dkEventId"), row["tttId"]): row for row in mapped}
    result = []
    history_reasons = {"no historical sample to estimate win probability",
                       "fewer than 30 completed prior head-to-head matches"}
    for report in reports:
        history = histories.get(report.get("tttId"))
        observed = (history or {}).get("observedHistoryCount")
        if type(observed) is not int:
            result.append(report)
            continue
        shown = {**report, "priorGames": observed, "decision": dict(report["decision"])}
        mapping = mappings.get((report.get("eventId"), report.get("tttId")), {})
        # A completed identity/score check is still valid when the *training*
        # minimum fails. Never mark an ambiguous, stale or conflicting map ready.
        if (source_fresh and mapping.get("reason") in history_reasons
                and mapping.get("completedSetScoresAgree") is True
                and mapping.get("tttScoreFresh") is True):
            shown["mappingVerified"] = True
        totals = history["totals"]
        fitted, origin = totals["totalMatches"], totals.get("trainingOrigin")
        if (fitted < 30 and observed > fitted and number(origin) is not None
                and report.get("mappingReason") in history_reasons):
            origin_text = datetime.fromtimestamp(origin, timezone.utc).strftime("%H:%M UTC")
            text = f"History fit warming up: {observed} clean prior H2H meetings found; {fitted} eligible in this match's frozen {origin_text} fit."
            next_origin = origin + 21600
            if now < next_origin:
                next_text = datetime.fromtimestamp(next_origin, timezone.utc).strftime("%H:%M UTC")
                text += f" New matches get the next fit from {next_text}."
            else:
                text += " This match keeps its assigned fit; new matches use later origins."
            shown["decision"]["reason"] = text
            if fitted == 0:
                shown["decision"]["candidates"] = []  # No fitted sample, rather than a misleading 0/0 H2H history.
        result.append(shown)
    return result


def validate_opportunity(value):
    """This schema contains no selection IDs, commands, or execution authority."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != FIELDS or value["protocol"] != 1:
        raise ValueError("Invalid opportunity display")
    identifier = value["event_id"]
    if identifier is not None and (not isinstance(identifier, str) or not identifier.isascii()
                                   or not identifier.isdigit() or len(identifier) > 32):
        raise ValueError("Invalid displayed event")
    players = value["players"]
    if not isinstance(players, list) or len(players) != 2 or any(
            not isinstance(player, str) or not 0 < len(player) <= 200 for player in players):
        raise ValueError("Invalid displayed participants")
    for field in ("set_tally", "points"):
        pair = value[field]
        if pair is not None and (not isinstance(pair, list) or len(pair) != 2
                                or any(type(item) is not int or not 0 <= item <= 999 for item in pair)):
            raise ValueError("Invalid displayed score")
    for field, limit in (("period", 40), ("market", 80), ("reason", 240)):
        if not isinstance(value[field], str) or len(value[field]) > limit:
            raise ValueError("Invalid opportunity description")
    if value["bet_type"] not in (None, "split", "complement", "closeout", "set_handicap", "point_handicap", "correct_score", "match_points") or value["ev_mode"] not in ("point", "conservative"):
        raise ValueError("Invalid displayed strategy")
    for field in ("mapping_verified", "entry_open", "qualifies"):
        if type(value[field]) is not bool:
            raise ValueError("Invalid opportunity state")
    if number(value["minimum_ev"]) is None or not 0 <= value["minimum_ev"] <= 1e6:
        raise ValueError("Invalid displayed EV minimum")
    if number(value["confidence"]) is None or not 50 <= value["confidence"] < 100:
        raise ValueError("Invalid displayed confidence")
    history = value["history_count"]
    if history is not None and (type(history) is not int or not 0 <= history <= 1_000_000):
        raise ValueError("Invalid displayed history")
    if not isinstance(value["candidates"], list) or len(value["candidates"]) > 4:
        raise ValueError("Invalid displayed candidates")
    for candidate in value["candidates"]:
        if not isinstance(candidate, dict) or set(candidate) != CANDIDATE_FIELDS:
            raise ValueError("Invalid displayed candidate")
        if (candidate["player"] not in players and (value['bet_type'] not in ('correct_score', 'match_points')
                or not isinstance(candidate['player'], str) or not 0 < len(candidate['player']) <= 420)):
            raise ValueError("Candidate does not belong to displayed match")
        if candidate['line'] is not None and (number(candidate['line']) is None or abs(candidate['line']) > 1e6):
            raise ValueError('Invalid displayed handicap or total')
        for field in ("probability", "lower_probability", "implied_probability"):
            item = candidate[field]
            if item is not None and (number(item) is None or not 0 <= item <= 1):
                raise ValueError("Invalid displayed probability")
        for field in ("estimated_ev", "conservative_ev", "gate_ev"):
            item = candidate[field]
            if item is not None and (number(item) is None or abs(item) > 1e9):
                raise ValueError("Invalid displayed EV")
        odds = candidate["decimal_odds"]
        if odds is not None and (number(odds) is None or not 1 < odds <= 1e6):
            raise ValueError("Invalid displayed odds")
        american = candidate["american_odds"]
        if american is not None and (not isinstance(american, str) or len(american) > 16
                                     or not american.lstrip("+-").isascii() or not american.lstrip("+-").isdigit()):
            raise ValueError("Invalid American odds")
        sample, successes = candidate["sample_size"], candidate["success_count"]
        if (type(sample) is not int or type(successes) is not int or not 0 <= successes <= sample <= 1_000_000
                or type(candidate["ev_passed"]) is not bool):
            raise ValueError("Invalid displayed conditional sample")
        if not value["entry_open"] and any(candidate[field] is not None for field in
                ("estimated_ev", "conservative_ev", "gate_ev")):
            raise ValueError("Closed start-state windows cannot display live EV")
    if value["qualifies"] and (not value["mapping_verified"] or not value["entry_open"]
                               or (history or 0) < 30 or not any(row["ev_passed"] for row in value["candidates"])):
        raise ValueError("Inconsistent displayed qualification")
    updated, expires = stamp(value["updated_at"]), stamp(value["expires_at"])
    if not timedelta(0) < expires-updated <= timedelta(seconds=15):
        raise ValueError("Invalid opportunity display lifetime")
    for field in ("score_at", "market_at"):
        if value[field] is not None:
            stamp(value[field])
    return value


def opportunity_snapshot(reports, events, preferred_event_id=None, qualified_events=(), now=None,
                         minimum_ev=0, confidence=95):
    """Prefer the prepared match; otherwise expose the best evaluated candidate.

    EV percentages are copied from evaluate_market, never rescaled or recomputed.
    A missing quote/sample or a closed start-state window stays unknown, not zero.
    """
    if not reports:
        return None
    now = now or datetime.now(timezone.utc)
    def rank(row):
        values = [candidate.get("gateNetEVPercent") for candidate in row.get("decision", {}).get("candidates", [])]
        known = [value for value in values if number(value) is not None]
        return (bool(row.get("eventId")), bool(known), max(known, default=float("-inf")))
    preferred = [row for row in reports if preferred_event_id is not None and row.get("eventId") == preferred_event_id]
    report = max(preferred or reports, key=rank)
    decision = report.get("decision") or {}
    event = events.get(report.get("eventId")) or {}
    players = [event.get("player1"), event.get("player2")] if event else report.get("players")
    if not players or len(players) != 2:
        return None
    entry_open = decision.get("entryWindowOpen") is True
    candidates = []
    rows = list(decision.get('candidates', []))
    selected = decision.get('selected')
    if selected is not None:
        rows = [selected] + [row for row in rows if row is not selected]
    for row in rows[:4]:
        context, outcome = row.get("context") or {}, row.get("outcome") or {}
        if context.get("selection") not in players and context.get('betType') not in ('correct_score', 'match_points'):
            continue
        implied = number(row.get("impliedProbability"))
        candidates.append({
            "player": context["selection"],
            "line": number(outcome.get('points')),
            "decimal_odds": 1/implied if implied and implied < 1 else None,
            "american_odds": str(outcome["american"]) if outcome.get("american") is not None else None,
            "probability": number(row.get("historicalProbability")),
            "lower_probability": number(row.get("lowerProbability")),
            "implied_probability": implied,
            "estimated_ev": number(row.get("estimatedNetEVPercent")) if entry_open else None,
            "conservative_ev": number(row.get("conservativeNetEVPercent")) if entry_open else None,
            "gate_ev": number(row.get("gateNetEVPercent")) if entry_open else None,
            "sample_size": context.get("conditioningCount", 0),
            "success_count": context.get("successCount", context.get("splitCount", 0)),
            "ev_passed": bool(entry_open and row.get("evPassed")),
        })
    first = (decision.get("candidates") or [{}])[0]
    current = event.get("currentSet") or {}
    points = [current.get("home"), current.get("away")]
    if any(type(point) is not int for point in points):
        points = decision.get("currentSetPoints")
    value = {
        "protocol": 1, "event_id": report.get("eventId"), "players": list(players),
        "set_tally": event.get("setTally"), "points": points,
        "period": event.get("period") or "", "market": decision.get("marketName") or "",
        "bet_type": decision.get("betType"),
        "reason": str(decision.get("reason") or report.get("marketWaitReason") or "Waiting for a verified opportunity")[:240],
        "mapping_verified": report.get("mappingVerified", report.get("mappingReason") == "eligible"),
        "entry_open": entry_open,
        "qualifies": report.get("eventId") in qualified_events,
        "minimum_ev": first.get("minimumEVPercent", minimum_ev),
        "ev_mode": decision.get("evMode", "conservative"),
        "confidence": first.get("confidencePercent", confidence),
        "history_count": report.get("priorGames"), "candidates": candidates,
        "updated_at": now.isoformat(), "expires_at": (now+timedelta(seconds=5)).isoformat(),
        "score_at": decision.get("scoreReceivedAt"), "market_at": decision.get("marketReceivedAt"),
    }
    return validate_opportunity(value)
