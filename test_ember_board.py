"""Wire/display boundary checks, independent of the private strategy engine."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from ember_board import display_board, validate_board
from ember_protocol import validate_scanner, validate_stage


def board():
    now = datetime.now(timezone.utc)
    outcome = {"selection_id": "s2", "label": "Two", "line": None, "decimal_odds": 2,
               "conservative_ev": 30, "entry_ev": 30, "entry_open": True, "quote_at": now.isoformat(),
               "lower_probabilities": {"90": 30, "95": 20}, "entry_evs": {"90": 30, "95": 20}}
    event = {"event_id": "100", "players": ["One", "Two"], "status": "STARTED", "set_tally": [1, 0],
             "points": [0, 0], "period": "2nd", "sets": [{"set": 1, "home": 11, "away": 5}],
             "score_at": now.isoformat(), "markets": [{"market_id": "m2", "name": "Game 2", "outcomes": [outcome]}]}
    return {"protocol": 1, "updated_at": now.isoformat(), "expires_at": (now+timedelta(seconds=15)).isoformat(), "events": [event]}


class BoardValidationTests(unittest.TestCase):
    def test_confidence_windows_are_independent_and_stale_evidence_stays_closed(self):
        original={'board':board()}
        outcome=original['board']['events'][0]['markets'][0]['outcomes'][0]
        outcome.update(entry_open=False,reference_windows={
            '90':{'kind':'pregame_non_decreasing','key':'a'*64,'status':'cancelled'},
            '95':{'kind':'pregame_non_decreasing','key':'b'*64,'status':'open'}})
        validate_board(original['board'])
        for level,opened,key in (('conservative',False,'a'),('very_conservative',True,'b')):
            selected=display_board(original,level)['board']['events'][0]['markets'][0]['outcomes'][0]
            self.assertEqual(selected['entry_open'],opened)
            self.assertEqual(selected['reference_window']['key'],key*64)
        outcome['lower_probabilities']['95']=None
        self.assertFalse(display_board(original,'very_conservative')['board']['events'][0]['markets'][0]['outcomes'][0]['entry_open'])
        outcome['reference_windows']['95']['status']='guessed'
        with self.assertRaises(ValueError):validate_board(original['board'])

    def test_display_uses_the_selected_server_confidence_without_deriving_probability(self):
        original = {"board": board()}
        for level, expected in (("conservative", 30), ("very_conservative", 20)):
            selected = display_board(original, level)["board"]["events"][0]["markets"][0]["outcomes"][0]
            self.assertEqual(selected["conservative_ev"], expected)
            self.assertEqual(selected["entry_ev"], expected)
        self.assertEqual(original["board"]["events"][0]["markets"][0]["outcomes"][0]["conservative_ev"], 30)

    def test_wire_relay_preserves_all_distinct_h2h_matches(self):
        value = board()
        second = deepcopy(value["events"][0]); second["event_id"] = "101"
        value["events"].append(second)
        result = validate_scanner({"healthy": True, "counts": {}, "board": value})
        self.assertEqual([e["event_id"] for e in result["board"]["events"]], ["100", "101"])
        self.assertEqual(result["stages"], [])

    def test_untrusted_board_never_escapes_its_size_numeric_or_identity_bounds(self):
        def outcome(v):
            return v["events"][0]["markets"][0]["outcomes"][0]
        mutations = [lambda v: v["events"].append(deepcopy(v["events"][0])),
                     lambda v: outcome(v).update(decimal_odds=float("nan")),
                     lambda v: outcome(v).update(decimal_odds=True),
                     lambda v: outcome(v).update(label="Wrong match"),
                     lambda v: outcome(v).update(lower_probabilities={"99": 10}),
                     lambda v: v["events"][0].update(points=[1, False]),
                     lambda v: v.update(expires_at=v["updated_at"])]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                value = board(); mutate(value)
                with self.assertRaises((ValueError, TypeError)):
                    validate_board(value)
                result = validate_scanner({"healthy": True, "counts": {}, "board": value})
                self.assertIsNone(result["board"])

    def test_catalog_is_identity_only_and_cannot_restore_complement(self):
        now = datetime.now(timezone.utc)
        event = {"eventId": "100", "player1": "One", "player2": "Two", "status": "STARTED"}
        hint = {"protocol": 1, "event_id": "100", "market_id": "m2", "selection_id": "s2", "label": "Two",
                "market_name": "Game 2", "bet_type": "split", "line": None, "event": event,
                "updated_at": now.isoformat(), "expires_at": (now+timedelta(seconds=5)).isoformat()}
        self.assertEqual(validate_scanner({"healthy": True, "stages": [hint]})["stages"], [hint])
        for bad in ({**hint, "stake": 1}, {**hint, "bet_type": "complement"},
                    {**hint, "event": {**event, "eventId": "101"}}, {**hint, "label": "Unknown"}):
            with self.subTest(hint=bad):
                with self.assertRaises(ValueError):
                    validate_stage(bad)
                self.assertEqual(validate_scanner({"healthy": True, "stages": [bad]})["stages"], [])


if __name__ == "__main__":
    unittest.main()
