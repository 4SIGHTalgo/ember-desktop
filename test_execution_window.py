from copy import deepcopy
import unittest

from execution_window import identity, points


def event():
    return {'eventId':'100','startTimestamp':1,'player1':'One','player2':'Two','status':'STARTED',
        'sets':[{'set':1,'home':11,'away':5},{'set':2,'home':0,'away':0}],
        'setTally':[1,0],'period':'2nd'}


class WindowScoreTests(unittest.TestCase):
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
