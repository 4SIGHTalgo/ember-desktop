"""Direct tests for the shared market-semantics module (protocol layer)."""
import unittest

import tt_market_semantics as semantics


def event(sets, tally, period="3rd", status="STARTED", **extra):
    return {"eventId": "10", "player1": "Piotr Zemla", "player2": "Tomasz Lojtek",
            "status": status, "period": period, "setTally": tally,
            "sets": [{"set": number, "home": home, "away": away} for number, (home, away) in enumerate(sets, 1)],
            **extra}


def state_event(prefix, points=(0, 0), **overrides):
    """The public fixture home: a verified in-play event at a completed-set prefix."""
    target = len(prefix) + 1
    rows = [{"set": n, "home": 11 if winner == "1" else 5,
             "away": 5 if winner == "1" else 11} for n, winner in enumerate(prefix, 1)]
    if target <= 5:
        rows.append({"set": target, "home": points[0], "away": points[1]})
    return event([(row["home"], row["away"]) for row in rows], [prefix.count("1"), prefix.count("2")],
                 period={2: "2nd", 3: "3rd", 4: "4th", 5: "5th"}.get(target, "5th"),
                 currentSet=rows[-1], **overrides)


TWO_DECISIVE_SETS = [(11, 9), (8, 11)]


class OddsTests(unittest.TestCase):
    def test_american_odds_converts_independently_derived_quotes(self):
        # Decimal payout: 1 + 100/|v| for favorites, 1 + v/100 for underdogs.
        for text, expected in (("-140", 1 + 100 / 140), ("+101", 1 + 101 / 100),
                               ("-105", 1 + 100 / 105), ("+250", 3.5), ("+100", 2.0)):
            with self.subTest(text=text):
                self.assertAlmostEqual(semantics.american_odds(text), expected, places=12)

    def test_american_odds_rejects_zero_and_non_numbers(self):
        for bad in ("0", "+0", "-0", None, "", "even", "14/5", "+10.5"):
            with self.subTest(bad=bad):
                self.assertIsNone(semantics.american_odds(bad))

    def test_price_text_normalizes_the_typographic_minus(self):
        for raw, expected in (("\u2212140", "-140"), ("\u2013101", "-101"),
                              (" +101 ", "+101"), (None, ""), (5, "5")):
            with self.subTest(raw=raw):
                self.assertEqual(semantics.price_text(raw), expected)

    def test_outcome_decimal_prefers_the_displayed_american_quote(self):
        self.assertAlmostEqual(semantics.outcome_decimal({"american": "+101", "decimal": 9.99}), 2.01)
        self.assertEqual(semantics.outcome_decimal({"decimal": 2.5}), 2.5)
        self.assertIsNone(semantics.outcome_decimal({}))

    def test_implied_probability_is_one_over_decimal_fail_closed(self):
        self.assertEqual(semantics.implied_probability(2.0), 0.5)
        for bad in (1, 0, -2.0, float("inf"), "2.0", None):
            with self.subTest(bad=bad):
                self.assertIsNone(semantics.implied_probability(bad))


class VerifiedScoreTests(unittest.TestCase):
    def test_completed_scores_keeps_only_the_decisive_prefix(self):
        self.assertEqual(semantics.completed_scores([{"set": 1, "home": 11, "away": 9}]), [(11, 9)])
        # A 9-7 set never reached 11 points, so it cannot complete a prefix.
        self.assertEqual(semantics.completed_scores([{"set": 1, "home": 9, "away": 7}]), [])
        # 11-10 is not a two-point game; the prefix stops before it.
        self.assertEqual(semantics.completed_scores([{"set": 1, "home": 11, "away": 10}]), [])

    def test_completed_scores_accepts_the_feed_dict_shape(self):
        self.assertEqual(semantics.completed_scores({"1": {"home": 11, "away": 9}}), [(11, 9)])

    def test_completed_scores_fails_closed_on_malformed_rows(self):
        for bad in (None, "sets", [{"set": 2, "home": 11, "away": 9}], [{"set": 1, "home": "?", "away": 9}],
                    [{"set": 1, "home": 11}], [7]):
            with self.subTest(bad=bad):
                self.assertIsNone(semantics.completed_scores(bad))

    def test_next_set_number_and_score_state_agree_with_the_tally(self):
        live = event(TWO_DECISIVE_SETS, [1, 1])
        self.assertEqual(semantics.next_set_number(live), 3)
        self.assertEqual(semantics.score_state(live), [1, 1])
        self.assertIsNone(semantics.score_state(event(TWO_DECISIVE_SETS, [2, 0])))

    def test_finished_events_have_no_next_set(self):
        for tally in ([3, 1], [2, 3]):
            with self.subTest(tally=tally):
                self.assertTrue(semantics.finished(event(TWO_DECISIVE_SETS, tally)))
                self.assertIsNone(semantics.next_set_number(event(TWO_DECISIVE_SETS, tally)))

    def test_closeout_state_requires_a_two_win_lead(self):
        periods = {2: "2nd", 3: "3rd", 4: "4th", 5: "5th"}
        for sets, tally, expected in ((((11, 9), (11, 9)), (2, 0), True),
                                      (((9, 11), (9, 11)), (0, 2), True),
                                      (((11, 9), (8, 11), (11, 9)), (2, 1), True),
                                      (TWO_DECISIVE_SETS, (1, 1), False)):
            with self.subTest(tally=tally):
                live = event(list(sets), list(tally), period=periods[len(sets) + 1])
                self.assertEqual(semantics.closeout_state(live) is not None, expected)

    def test_entry_window_is_open_only_before_the_first_point(self):
        with self.subTest(case="between sets, feed still names the finished period"):
            self.assertTrue(semantics.entry_window(event(TWO_DECISIVE_SETS, [1, 1], period="2nd")))
        with self.subTest(case="current set already started"):
            live = event(TWO_DECISIVE_SETS, [1, 1], currentSet={"set": 3, "home": 1, "away": 0})
            self.assertFalse(semantics.entry_window(live))
        with self.subTest(case="target listed in closed games"):
            live = event(TWO_DECISIVE_SETS, [1, 1], entryClosedGames=[3])
            self.assertFalse(semantics.entry_window(live))
        with self.subTest(case="zero-zero current set still open"):
            live = event(TWO_DECISIVE_SETS, [1, 1], currentSet={"set": 3, "home": 0, "away": 0})
            self.assertTrue(semantics.entry_window(live))


class NameTests(unittest.TestCase):
    def test_name_key_folds_unicode_diacritics_and_case(self):
        self.assertEqual(semantics.name_key("Zoë"), "zoe")
        self.assertEqual(semantics.name_key("Łukasz Ødegård"), "lukasz odegard")
        self.assertEqual(semantics.name_key("Anne-Marie  O'Brien"), "anne marie o brien")

    def test_pair_key_is_orientation_independent(self):
        self.assertEqual(semantics.pair_key({"player1": "Alice", "player2": "Bob"}),
                         semantics.pair_key({"player1": "Bob", "player2": "Álice"}))


class PayoffKeyTests(unittest.TestCase):
    """Hand-derived partitions; the ledger's economic-dedup contract."""

    def test_missing_specification_fails_closed(self):
        self.assertIsNone(semantics.payoff_key({}, []))
        self.assertEqual(semantics.payoff_key({"betType": "weird", "specification": {"family": "weird"}}, []),
                         ("weird",))

    def test_a_decided_prefix_makes_the_partition_determined(self):
        # Three completed away wins: the only terminal sequence is (0, 0, 0),
        # so a close-out for player 0 is already settled in every branch.
        decided = {"betType": "closeout", "specification": {"family": "closeout", "player": 0}}
        self.assertEqual(semantics.payoff_key(decided, [(9, 11), (9, 11), (9, 11)]), ("determined",))

    def test_game_five_split_and_the_same_side_closeout_partition_identically(self):
        # From 2-2 (sequence (1, 0, 1, 0)) the terminals are exactly two:
        # (…, 0, 0) and (…, 1). "Player 1 wins game 5" (split) and "player 1
        # reaches three set wins" (close-out) select the same one, and the
        # canonical min() merges opposite sides into one economic identity.
        prefix = [(11, 9), (8, 11), (11, 9), (8, 11)]
        split = {"betType": "split", "specification": {"family": "split", "player": 1, "target": 5}}
        closeout = {"betType": "closeout", "specification": {"family": "closeout", "player": 1}}
        other_side = {"betType": "closeout", "specification": {"family": "closeout", "player": 0}}
        # The sequence records home losses (h < a), so this 2-2 is (0, 1, 0, 1).
        self.assertEqual(semantics.payoff_key(split, prefix), ("winner-event", (0, 1, 0, 1), (False, True)))
        self.assertEqual(semantics.payoff_key(split, prefix), semantics.payoff_key(closeout, prefix))
        self.assertEqual(semantics.payoff_key(split, prefix), semantics.payoff_key(other_side, prefix))
        self.assertNotEqual(semantics.payoff_key(split, prefix),
                            semantics.payoff_key(split, [(11, 9), (11, 9), (8, 11), (8, 11)]))

    def test_terminal_sequences_stop_as_soon_as_either_side_reaches_three(self):
        self.assertEqual(semantics.terminal_sequences((0, 0, 0)), ((0, 0, 0),))
        self.assertEqual(semantics.terminal_sequences((1, 1)),
                         ((1, 1, 0, 0, 0), (1, 1, 0, 0, 1), (1, 1, 0, 1), (1, 1, 1)))


class MarketMatchTests(unittest.TestCase):
    def test_split_context_matches_only_its_verified_next_game(self):
        for name, next_set, expected in (("Game 3", 3, True), ("Game 2", 3, False),
                                         ("Moneyline", 5, True), ("Game 5", 5, True)):
            with self.subTest(name=name):
                self.assertEqual(semantics.context_matches_market(
                    {"betType": "split", "nextSet": next_set}, {"name": name}), expected)

    def test_closeout_context_matches_only_the_moneyline(self):
        self.assertTrue(semantics.context_matches_market({"betType": "closeout"}, {"name": "Moneyline"}))
        self.assertFalse(semantics.context_matches_market({"betType": "closeout"}, {"name": "Game 3"}))

    def test_bet_selection_requires_one_matching_quote(self):
        market = {"name": "Game 3", "outcomes": [
            {"selectionId": "a", "label": "Tomasz Lojtek"},
            {"selectionId": "b", "label": "Piotr Zemla"}]}
        picked = semantics.bet_selection({"betType": "split", "nextSet": 3,
                                          "conditioningCount": 40, "selection": "Tomasz Lojtek"}, market)
        self.assertEqual(picked["selectionId"], "a")
        self.assertIsNone(semantics.bet_selection({"betType": "split", "nextSet": 3,
                                                   "conditioningCount": 40,
                                                   "selection": "Nobody"}, market))
        self.assertIsNone(semantics.bet_selection({"betType": "split", "nextSet": 1,
                                                    "conditioningCount": 40,
                                                    "selection": "Tomasz Lojtek"}, market))
        self.assertIsNone(semantics.bet_selection(None, market))


if __name__ == "__main__":
    unittest.main()
