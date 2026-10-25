"""The scanner opportunity view is display-only wire data; its schema is the contract."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from ember_opportunity import opportunity_snapshot, validate_opportunity
from ember_protocol import validate_scanner


def make_snapshot(decimal=2.0, closed=False, qualified=True):
    """A synthetic, schema-valid view; no strategy module produced it."""
    now = datetime.now(timezone.utc)
    candidate = {"player": "Tomasz Lojtek", "decimal_odds": decimal, "american_odds": "+100",
                 "probability": .6, "lower_probability": .55, "implied_probability": .5,
                 "estimated_ev": 20.0, "conservative_ev": 10.0, "gate_ev": 10.0,
                 "sample_size": 30, "success_count": 18, "ev_passed": True, "line": None}
    if closed:
        candidate.update(estimated_ev=None, conservative_ev=None, gate_ev=None)
    view = {"protocol": 1, "event_id": "10", "players": ["Piotr Zemla", "Tomasz Lojtek"],
            "set_tally": [1, 0], "points": None, "period": "2nd", "market": "Game 2",
            "bet_type": "split", "reason": "eligible", "mapping_verified": True,
            "entry_open": not closed, "qualifies": qualified and not closed,
            "minimum_ev": 0, "ev_mode": "conservative", "confidence": 95, "history_count": 30,
            "candidates": [candidate],
            "updated_at": now.isoformat(), "expires_at": (now+timedelta(seconds=15)).isoformat(),
            "score_at": None, "market_at": None}
    validate_opportunity(view)
    return view, [candidate], None


class OpportunityDisplayTests(unittest.TestCase):
    def test_display_only_schema_rejects_actions_nonfinite_values_and_foreign_players(self):
        original, _, _ = make_snapshot()
        for mutate in (
            lambda value: value.update(submit=True),
            lambda value: value["candidates"][0].update(gate_ev=float("nan")),
            lambda value: value["candidates"][0].update(player="Someone else"),
            lambda value: value["candidates"][0].update(probability=True),
            lambda value: value.update(qualifies=True, entry_open=False),
        ):
            value = deepcopy(original)
            mutate(value)
            with self.assertRaises(ValueError):
                validate_opportunity(value)

    def test_malformed_display_does_not_invalidate_scanner_metrics(self):
        view, _, _ = make_snapshot()
        view["execute"] = "not allowed"
        scanner = validate_scanner({"healthy": True, "counts": {"events": 3}, "opportunity": view})
        self.assertEqual(scanner["counts"], {"events": 3})
        self.assertIsNone(scanner["opportunity"])

    def test_no_report_means_unknown_opportunity_not_a_zero_ev_placeholder(self):
        self.assertIsNone(opportunity_snapshot([], {}))

    def test_qualification_requires_verified_mapping_an_open_window_and_a_passing_candidate(self):
        for mutate in (
            lambda value: value.update(mapping_verified=False),
            lambda value: value.update(history_count=29),
            lambda value: value["candidates"][0].update(ev_passed=False),
            lambda value: value.update(candidates=[]),
        ):
            with self.subTest(mutate=mutate.__name__):
                value, _, _ = make_snapshot()
                mutate(value)
                with self.assertRaises(ValueError):
                    validate_opportunity(value)

    def test_closed_start_state_windows_never_display_live_ev(self):
        value, _, _ = make_snapshot(closed=True)
        value["candidates"][0].update(gate_ev=10.0)
        with self.assertRaises(ValueError):
            validate_opportunity(value)


if __name__ == "__main__":
    unittest.main()
