from copy import deepcopy
import unittest

import draftkings_live as live
from execution_window import identity, points


def event():
    return {'eventId':'100','startTimestamp':1,'player1':'One','player2':'Two','status':'STARTED',
        'sets':[{'set':1,'home':11,'away':5},{'set':2,'home':0,'away':0}],
        'setTally':[1,0],'period':'2nd'}


def wire_event(period, tally, rows):
    """A DK wire event as captured live (34901204, 2026-10-27 00:00Z)."""
    card = [[None, None, 2147483647, *tally, None, None, None],
            [[None, str(n), n, *score, None, None] for n, score in enumerate(rows, 1)], 1]
    return live.event_record(['34901204', '26', '208037', 'TT Elite Series', 'A vs B', None, 'Artur Szoltysek',
                              'Mariusz Zwolinski', ['STARTED', False, period, 0, 0, False, *tally], [], False, card])


class WindowScoreTests(unittest.TestCase):
    def test_between_sets_the_finished_set_reported_as_current_is_the_zero_zero_anchor(self):
        blank = [('', '')]*3
        # 00:00:16.8: set 1 ended 9-11, period still 1st; 00:00:33.3: period 2nd; 00:00:46.4: first point.
        for period, second, expected in (('1st', ('0', '0'), (0, 0)), ('2nd', ('0', '0'), (0, 0)),
                                         ('2nd', ('0', '1'), (0, 1)), ('1st', ('0', '1'), None)):
            with self.subTest(period=period, second=second):
                row = wire_event(period, ('0', '1'), [('9', '11'), second, *blank])
                self.assertEqual(points(row, 2, [0, 1]), expected)
        row = wire_event('1st', ('0', '1'), [('9', '11'), ('0', '0'), *blank])
        self.assertEqual(row['currentSet'], {'set': 1, 'home': 9, 'away': 11})
        for mutate in (lambda row: row.update(currentSet={'set': 1, 'home': 9, 'away': 10}),
                       lambda row: row.update(currentSet={'set': 1, 'home': 11, 'away': 9}),
                       lambda row: row.update(period='3rd'), lambda row: row.update(setTally=[1, 1])):
            with self.subTest(mutate=mutate):
                changed = deepcopy(row); mutate(changed)
                self.assertIsNone(points(changed, 2, [0, 1]))
        self.assertIsNone(points(wire_event('1st', ('0', '0'), [('9', '10'), *blank]), 2, [0, 1]))

    def test_points_do_not_close_an_anchored_set_but_completion_or_ambiguity_does(self):
        for pair in ((0,0),(1,0),(10,10),(12,11)):
            with self.subTest(pair=pair):
                row=event();row['sets'][1].update(home=pair[0],away=pair[1])
                self.assertEqual(points(row,2,[1,0]),pair)
        for mutate in (lambda row:row['sets'][1].update(home=11,away=9),
                       lambda row:row.update(setTally=[2,0]),lambda row:row.update(period='3rd'),
                       lambda row:row.update(currentSet={'set':2,'home':1,'away':0})):
            with self.subTest(mutate=mutate):
                row=event();mutate(row);self.assertIsNone(points(row,2,[1,0]))

    def test_window_identity_excludes_live_points_and_quotes(self):
        row=event();context={'selectionId':'s','betType':'split','nextSet':2,'scoreState':[1,0],
            'specification':{'family':'split','player':1,'target':2}}
        market={'marketId':'m','outcomes':[{'decimal':2}]}
        key=identity(row,market,context);changed=deepcopy(row);changed['sets'][1]['away']=3
        self.assertEqual(key,identity(changed,{**market,'outcomes':[{'decimal':3}]},context))
        changed['sets'][0]['home']=12
        self.assertNotEqual(key,identity(changed,market,context))
