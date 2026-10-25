"""The five enabled production families; pure policy tables."""
import unittest

from execution_policy import (ALLOWED_FAMILIES, DISTRIBUTION_MARKETS, EXCLUDED_FAMILIES,
                              EXCLUDED_MARKETS, MARKET_FAMILIES, allows_bet)


class FiveFamilyPolicyTests(unittest.TestCase):
    def test_exactly_five_families_and_only_positive_one_point_five_sets(self):
        self.assertEqual(ALLOWED_FAMILIES, {'split', 'complement', 'closeout', 'set_handicap', 'point_handicap'})
        for family in ('split', 'complement'):
            for target in (2, 3, 4, 5):
                self.assertTrue(allows_bet({'betType': family, 'nextSet': target}, {'name': f'Game {target}'}))
        self.assertTrue(allows_bet({'betType': 'closeout'}, {'name': 'Moneyline'}))
        self.assertTrue(allows_bet({'betType': 'point_handicap'}, {'name': 'Points Spread'}))
        for line in (1.5, -1.5, 2.5, None):
            self.assertEqual(allows_bet({'betType': 'set_handicap', 'specification': {'line': line}},
                {'name': 'Games Spread'}, {'points': line}), line == 1.5)
        self.assertFalse(allows_bet({'betType': 'set_handicap', 'specification': {'line': 1.5}},
            {'name': 'Games Spread'}, {'points': -1.5}))

    def test_market_families_cover_every_named_market_exactly_once(self):
        self.assertEqual(set(MARKET_FAMILIES), set(DISTRIBUTION_MARKETS) | EXCLUDED_MARKETS)
        self.assertEqual(set(MARKET_FAMILIES.values()),
                         set(DISTRIBUTION_MARKETS.values()) | EXCLUDED_FAMILIES)
        self.assertEqual(MARKET_FAMILIES['Games Spread'], 'set_handicap')
        self.assertEqual(MARKET_FAMILIES['Points Spread'], 'point_handicap')


if __name__ == "__main__":
    unittest.main()
