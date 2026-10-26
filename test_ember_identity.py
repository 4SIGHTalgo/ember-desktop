from copy import deepcopy
import unittest

from ember_identity import name_key, observation_identity, provider_id, signal_identity


class IdentityTests(unittest.TestCase):
    def test_provider_identifiers_preserve_real_opaque_values(self):
        for kind, value in (("event", "00100"), ("market", "1_86910413"),
                            ("selection", "0QA382420377#2334874376_13L208037Q1665207144Q20")):
            with self.subTest(kind=kind):
                self.assertTrue(provider_id(value, kind))
        for value in (None, True, 1, "", "1/2", "1 2", "１", "1\n", '1"', "1%2", "a"*201):
            for kind in ("event", "market", "selection"):
                with self.subTest(kind=kind, value=value):
                    self.assertFalse(provider_id(value, kind))
        self.assertNotEqual("00100", "100")

    def test_observation_changes_are_not_execution_changes(self):
        original = {"protocol": 2, "id": 7, "contract": "100:closeout", "payload": {
            "event": {"eventId": "100", "receivedAt": "old", "publishedAt": "old"},
            "market": {"marketId": "1_86910413", "outcomes": [
                {"selectionId": "a", "decimal": 2}, {"selectionId": "b", "decimal": 3}]},
            "plan": {"deadline": "old", "selectionId": "a", "min_decimal": 1.9}}}
        changed = deepcopy(original)
        changed["payload"]["event"].update(receivedAt="new", publishedAt="new", type="score")
        changed["payload"]["market"]["outcomes"].reverse()
        changed["payload"]["plan"]["deadline"] = "new"
        self.assertEqual(signal_identity(original), signal_identity(changed))
        changed["payload"]["market"]["outcomes"][0]["decimal"] = 4
        self.assertNotEqual(signal_identity(original), signal_identity(changed))
        self.assertEqual(original["payload"]["plan"]["deadline"], "old")
        self.assertEqual(name_key("Łukasz Oracz"), name_key("Lukasz Oracz"))
        self.assertEqual(observation_identity({"receivedAt": "new", "score": [1, 0]}), {"score": [1, 0]})


if __name__ == "__main__":
    unittest.main()
