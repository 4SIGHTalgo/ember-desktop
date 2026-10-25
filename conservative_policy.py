"""Shared production policy and economic contract identity."""
import math

LEVELS = {"conservative": 90, "very_conservative": 95}
DEFAULT_LEVEL = "very_conservative"
DEFAULT_KELLY = .25
DEFAULT_RISK_PERCENT = 5.0
SERVER_SETTINGS = {"ev": 0, "confidence": 90, "cost": 0, "ev_mode": "conservative"}


def client_settings(level=DEFAULT_LEVEL):
    if level not in LEVELS:
        raise ValueError("Invalid conservative level")
    return {**SERVER_SETTINGS, "confidence": LEVELS[level]}


def validate_risk(percent, multiplier):
    if type(percent) not in (int, float) or not math.isfinite(percent) or not 0 < percent <= 100:
        raise ValueError("Max bankroll risk must be greater than 0 and at most 100 percent")
    if type(multiplier) not in (int, float) or multiplier not in (.25, .5):
        raise ValueError("Choose quarter Kelly or half Kelly")


def contract_target(context):
    if context.get("betType") in {"set_handicap", "point_handicap", "correct_score", "match_points"}:
        return context["betType"]
    # A match Moneyline remains the same contract at every score. At 2-2,
    # Game 5 and Moneyline settle on the very same winner.
    return "closeout" if context.get("betType") == "closeout" or context.get("nextSet") == 5 else context.get("nextSet", 2)


def contract_id(event, context):
    target = contract_target(context)
    suffix = "split" if target == 2 else 'game-' + str(target) if type(target) is int else target
    return f"{event['eventId']}:{suffix}"
