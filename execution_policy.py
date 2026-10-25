"""Enabled production families; historical grading remains separate."""

ALLOWED_FAMILIES = frozenset({'split', 'closeout', 'set_handicap', 'point_handicap'})
DISTRIBUTION_MARKETS = {'Games Spread': 'set_handicap', 'Points Spread': 'point_handicap'}
DISTRIBUTION_FAMILIES = frozenset(DISTRIBUTION_MARKETS.values())
# Provider market names carry no strategy; surface navigation needs all four.
MARKET_FAMILIES = {**DISTRIBUTION_MARKETS, 'Correct Score': 'correct_score', 'Total Points': 'match_points'}
EXCLUDED_FAMILIES = frozenset({'correct_score', 'match_points'})
EXCLUDED_MARKETS = frozenset({'Correct Score', 'Total Points'})


def allows_bet(context, market, outcome=None):
    """An allowed family must agree with its market and selected handicap."""
    if not isinstance(context, dict) or not isinstance(market, dict):
        return False
    family, name = context.get('betType'), market.get('name')
    if family not in ALLOWED_FAMILIES:
        return False
    if family in {'split', 'complement'}:
        target = context.get('nextSet')
        return type(target) is int and target in (2, 3, 4, 5) and (
            name == f'Game {target}' or target == 5 and name == 'Moneyline')
    if family == 'closeout':
        return name == 'Moneyline'
    if DISTRIBUTION_MARKETS.get(name) != family:
        return False
    if family == 'set_handicap':
        spec = context.get('specification')
        return isinstance(spec, dict) and spec.get('line') == 1.5 and (outcome is None or outcome.get('points') == 1.5)
    return True


def allows_payload(payload):
    return (isinstance(payload, dict) and isinstance(payload.get('outcome'), dict)
            and allows_bet(payload.get('context'), payload.get('market'), payload['outcome']))
