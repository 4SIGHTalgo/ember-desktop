from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ember_history import OpportunityJournal


def history_signal(identifier=1, score=None, contract="100:split"):
    now = datetime.now(timezone.utc)
    return {"id": identifier, "protocol": 1, "contract": contract, "expires_at": (now+timedelta(seconds=5)).isoformat(),
        "payload": {"event": {"eventId": contract.split(":")[0], "player1": "One", "player2": "Two", "setTally": score or [1, 0]},
            "market": {"marketId": "200", "eventId": contract.split(":")[0], "name": "Game 2"},
            "outcome": {"selectionId": "selection", "decimal": 1.6, "label": "Two"},
            "context": {"selection": "Two", "scoreState": score or [1, 0], "conditioningCount": 10,
                        "successCount": 8, "historicalMeetings": 50, "betType": "split", "nextSet": 2},
            "settings": {"ev": 17.5, "ev_mode": "point", "confidence": 95, "cost": 0}}}


class OpportunityJournalTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = self.directory/"opportunities.sqlite3"
        self.journal = OpportunityJournal(self.path)
        self.scope = ("https://example.com", "1")

    def snapshot(self):
        return self.journal.snapshot(self.scope)

    def test_repeated_quote_updates_create_one_record_per_entry_window(self):
        first, second = history_signal(), history_signal(2)
        identifier = self.journal.observe(self.scope, first, "login_required")
        self.assertEqual(self.journal.observe(self.scope, second, "login_required"), identifier)
        self.assertEqual(len(self.snapshot()["records"]), 1)
        self.journal.close_pending(self.scope, now=(datetime.now(timezone.utc)+timedelta(seconds=10)).isoformat(), expired_only=True)
        row = self.snapshot()["records"][0]
        self.assertEqual(row["outcome"], "missed")
        self.assertEqual(row["reason"], "login_required")
        self.assertAlmostEqual(row["snapshot"]["point_ev"], 28)
        self.assertEqual(row["signal_id"], 2)
        self.assertEqual(self.snapshot()["queued"], 1)

    def test_different_entry_windows_are_separate_observations(self):
        self.journal.observe(self.scope, history_signal(score=[2, 0], contract="100:closeout"))
        self.journal.observe(self.scope, history_signal(2, score=[2, 1], contract="100:closeout"))
        self.assertEqual(len(self.snapshot()["records"]), 2)

    def test_fresh_queued_signal_updates_expiry_and_diagnostics_remain_server_compatible(self):
        first = history_signal()
        self.journal.observe(self.scope, first)
        self.journal.transition(self.scope, first, 'processing', 'waiting')
        latest = history_signal(identifier=2)
        latest['expires_at'] = (datetime.now(timezone.utc)+timedelta(seconds=8)).isoformat()
        self.journal.transition(self.scope, latest, 'processing', 'waiting')
        self.journal.transition(self.scope, latest, 'missed', 'market_unavailable', result={
            'readiness': {'status': 'not_ready', 'reason': 'market section missing', 'attempts': 2, 'wait_ms': 350}})
        row = self.snapshot()['records'][0]
        self.assertEqual(row['signal_id'], 2)
        self.assertEqual(row['expires_at'], latest['expires_at'])
        self.assertEqual(row['readiness']['reason'], 'market section missing')
        self.assertEqual(self.journal.pending_batch(self.scope)[0]['readiness']['reason'], 'market surface not ready')
        self.assertEqual(self.snapshot()['records'][0]['readiness']['reason'], 'market section missing')

    def test_uncertain_submission_is_not_reclassified_as_missed_on_disconnect_or_expiry(self):
        signal = history_signal()
        self.journal.observe(self.scope, signal)
        self.journal.transition(self.scope, signal, "processing", "waiting")
        self.journal.transition(self.scope, signal, "submitting", "waiting")
        self.journal.transition(self.scope, signal, "missed", "connection_lost")
        self.journal.close_pending(self.scope, "connection_lost")
        self.assertEqual(self.snapshot()["counts"], {"uncertain": 1})
        self.assertEqual(self.snapshot()["records"][0]["reason"], "submission_uncertain")

    def test_closed_pick_diagnostics_survive_locally_without_breaking_server_sync(self):
        for number,reason in enumerate(('unsupported leg layout','closed picks could not be removed',
                                       'closed leg funding is unverified'),10):
            with self.subTest(reason=reason):
                signal=history_signal(number,contract=f'{number}:split')
                self.journal.observe(self.scope,signal)
                self.journal.transition(self.scope,signal,'missed','signal_expired',result={
                    'readiness':{'status':'not_ready','reason':reason,'attempts':0,'wait_ms':0}})
                self.assertEqual(self.snapshot()['records'][0]['readiness']['reason'],reason)
                self.assertTrue(all(row['readiness']['reason']=='market surface not ready'
                                    for row in self.journal.pending_batch(self.scope)))

    def test_explicit_non_submission_can_be_logged_as_missed_after_final_validation(self):
        signal = history_signal()
        self.journal.observe(self.scope, signal)
        self.journal.transition(self.scope, signal, "submitting", "waiting")
        self.journal.transition(self.scope, signal, "missed", "odds_below_ev", result={
            "submitted": False, "reason": "final slip price no longer meets the EV minimum",
            "comparison": {"estimatedNetEVPercent": -4.0, "slipDecimal": 1.2}, "receipt": "PRIVATE"})
        row = self.snapshot()["records"][0]
        self.assertEqual((row["outcome"], row["reason"]), ("missed", "odds_below_ev"))
        self.assertEqual(row["final_point_ev"], -4)
        self.assertNotIn("PRIVATE", json.dumps(row))

    def test_placed_receipt_wins_over_a_provisional_uncertain_outcome(self):
        signal = history_signal()
        self.journal.observe(self.scope, signal)
        self.journal.transition(self.scope, signal, "submitting", "waiting")
        self.journal.transition(self.scope, signal, "uncertain", "submission_uncertain")
        self.journal.transition(self.scope, signal, "placed", "placed")
        self.journal.close_pending(self.scope, "signal_expired")
        self.assertEqual(self.snapshot()["counts"], {"placed": 1})

    def test_revision_acknowledgement_cannot_drop_an_update_that_happened_during_upload(self):
        first = history_signal()
        self.journal.observe(self.scope, first)
        self.journal.transition(self.scope, first, "missed", "connection_lost")
        old_batch = self.journal.pending_batch(self.scope)
        self.journal.observe(self.scope, history_signal(2))
        self.journal.close_pending(self.scope, "signal_expired")
        self.journal.acknowledge(self.scope, old_batch)
        self.assertEqual(self.snapshot()["queued"], 1)
        self.journal.acknowledge(self.scope, self.journal.pending_batch(self.scope))
        self.assertEqual(self.snapshot()["queued"], 0)

    def test_records_survive_restart_and_recovery_distinguishes_unsubmitted_and_in_flight(self):
        first, second = history_signal(), history_signal(2, contract="200:split")
        self.journal.observe(self.scope, first)
        self.journal.observe(self.scope, second)
        self.journal.transition(self.scope, second, "submitting", "waiting")
        restarted = OpportunityJournal(self.path)
        restarted.recover(self.scope, self.directory/"not-present.sqlite3")
        self.assertEqual(restarted.snapshot(self.scope)["counts"], {"missed": 1, "uncertain": 1})

    def test_existing_execution_reservation_prevents_a_false_missed_record(self):
        signal = history_signal()
        self.journal.observe(self.scope, signal)
        ledger = self.directory/"execution.sqlite3"
        connection = sqlite3.connect(ledger)
        connection.execute("CREATE TABLE attempts(account TEXT,contract TEXT,status TEXT)")
        connection.execute("INSERT INTO attempts VALUES('1','100:split','reserved_or_unconfirmed')")
        connection.commit()
        connection.close()
        self.journal.close_pending(self.scope, "connection_lost", ledger_path=ledger)
        self.assertEqual(self.snapshot()["counts"], {"uncertain": 1})

    def test_origins_and_accounts_are_isolated_and_wire_payload_has_no_raw_snapshot(self):
        signal = history_signal()
        self.journal.observe(self.scope, signal)
        self.journal.transition(self.scope, signal, "missed", "signal_expired")
        other = (self.scope[0], "2")
        self.assertEqual(self.journal.pending_batch(other), [])
        self.journal.acknowledge(other, self.journal.pending_batch(self.scope))
        self.assertEqual(self.snapshot()["queued"], 1)
        batch = self.journal.pending_batch(self.scope)
        self.assertNotIn("snapshot", batch[0])
        self.assertNotIn("account", batch[0])
        self.assertEqual(self.journal.snapshot(("https://other.example", "1"))["records"], [])

    def test_later_quotes_cannot_erase_a_missed_attempt_that_already_consumed_its_contract(self):
        signal = history_signal()
        self.journal.observe(self.scope, signal)
        self.journal.transition(self.scope, signal, "processing", "waiting")
        self.journal.transition(self.scope, signal, "missed", "odds_below_ev", result={"submitted": False, "reason": "price no longer meets the EV gate"})
        newer = history_signal(2)
        self.journal.observe(self.scope, newer, reserved=True)
        self.journal.transition(self.scope, newer, "handled", "already_attempted")
        row = self.snapshot()["records"][0]
        self.assertEqual((row["signal_id"], row["outcome"], row["reason"]), (1, "missed", "odds_below_ev"))

    def test_old_missed_entries_are_queryable_even_after_many_other_observations(self):
        for identifier in range(1, 7):
            signal = history_signal(identifier, contract=f"{identifier}:split")
            self.journal.observe(self.scope, signal)
            self.journal.transition(self.scope, signal, "missed" if identifier <= 3 else "placed", "signal_expired" if identifier <= 3 else "placed")
        first = self.journal.snapshot(self.scope, limit=2, outcome="missed")
        self.assertEqual(first["total"], 3)
        self.assertEqual(first["next_offset"], 2)
        older = self.journal.snapshot(self.scope, limit=2, outcome="missed", offset=2)
        self.assertEqual(len(older["records"]), 1)
        self.assertEqual(older["records"][0]["signal_id"], 1)

    def test_two_journal_instances_do_not_duplicate_or_lose_the_entry_window(self):
        from concurrent.futures import ThreadPoolExecutor
        second = OpportunityJournal(self.path)
        journals = [self.journal, second]
        def observe(index):
            return journals[index % 2].observe(self.scope, history_signal(index+1))
        with ThreadPoolExecutor(max_workers=4) as pool:
            identifiers = list(pool.map(observe, range(16)))
        self.assertEqual(len(set(identifiers)), 1)
        self.assertEqual(len(self.snapshot()["records"]), 1)

    def test_later_real_execution_can_replace_a_read_only_observation(self):
        signal = history_signal()
        self.journal.observe(self.scope, signal)
        self.journal.transition(self.scope, signal, "observed_only", "read_only")
        self.journal.transition(self.scope, signal, "processing", "waiting")
        self.journal.transition(self.scope, signal, "placed", "placed")
        self.assertEqual(self.snapshot()["counts"], {"placed": 1})

    def test_clock_offset_adjustment_cannot_make_report_timestamps_go_backwards(self):
        now = datetime.now(timezone.utc)
        self.journal.observe(self.scope, history_signal(), now=now.isoformat())
        self.journal.observe(self.scope, history_signal(2), now=(now-timedelta(seconds=1)).isoformat())
        self.journal.transition(self.scope, history_signal(2), "missed", "signal_expired", now=(now-timedelta(seconds=1)).isoformat())
        row = self.snapshot()["records"][0]
        self.assertGreaterEqual(row["last_observed_at"], row["observed_at"])
        self.assertGreaterEqual(row["resolved_at"], row["last_observed_at"])

    def test_readiness_diagnostics_are_bounded_and_do_not_upload_arbitrary_browser_data(self):
        signal = history_signal()
        self.journal.observe(self.scope, signal)
        self.journal.transition(self.scope, signal, "missed", "market_unavailable", result={"readiness": {
            "status": "not_ready", "reason": "expanding winner section", "attempts": 4, "wait_ms": 1000,
            "page_body": "PRIVATE_BROWSER_DATA"}})
        batch = self.journal.pending_batch(self.scope)
        self.assertEqual(batch[0]["readiness"], {"status": "not_ready", "reason": "expanding winner section", "attempts": 4, "wait_ms": 1000})
        self.assertNotIn("PRIVATE_BROWSER_DATA", json.dumps(batch))


if __name__ == "__main__":
    unittest.main()
