import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from sky_scanner.bq import SPECS, RowsRejected
from sky_scanner.heard import Heard
from sky_scanner.ingest import Ingestor, Sender, interval_row, load_heard, snapshot_row
from sky_scanner.spool import Spool

WRITTEN = "2026-10-19T12:00:00+00:00"


def observation(t=1, address="ABC123"):
    return {
        "received_at": t,
        "receiver_id": "rx1",
        "address": address,
        "address_type": "icao",
        "frequency_hz": 1_090_000_000,
        "fields": {"callsign": "TEST1"},
    }


def interval(name):
    return {
        "receiver_id": "rx1",
        "frequency_hz": 1_090_000_000,
        "started_at": 1,
        "ended_at": 31,
        "decoder_version": name,
    }


def tables(spool):
    """Rows put into a mock spool, by table."""
    result = {}
    for call in spool.put.call_args_list:
        table, rows = call.args
        result.setdefault(table, []).extend(rows)
    return result


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.spool = MagicMock()
        self.ingestor = Ingestor(self.spool)

    def event(self, identity, kind="observation", data=None):
        return {"id": identity, "type": kind, "data": data if data is not None else observation()}

    def test_validates_entire_batch_before_mutating_state(self):
        with self.assertRaisesRegex(ValueError, "unknown event"):
            self.ingestor.accept([self.event("ok"), self.event("bad", kind="unknown")])
        self.assertEqual(self.ingestor.seen, {})
        self.assertEqual(self.ingestor.accumulator._buckets, {})

    def test_deduplicates_events_and_preserves_late_drop_behavior(self):
        event = self.event("same")
        self.ingestor.accept([event, event])
        self.assertEqual(next(iter(self.ingestor.accumulator._buckets.values())).message_count, 1)
        self.ingestor.persist(final=True)
        self.assertEqual(len(tables(self.spool)["aircraft_snapshots"]), 1)
        self.ingestor.accept([event])
        self.assertEqual(self.ingestor.accumulator._buckets, {})

    def test_spools_snapshots_and_intervals_with_one_written_at(self):
        self.ingestor.accept([self.event("o"), self.event("i", "interval", interval("v1"))])
        self.ingestor.persist(final=True)
        spooled = tables(self.spool)
        (snapshot,) = spooled["aircraft_snapshots"]
        (turn,) = spooled["capture_intervals"]
        self.assertEqual(snapshot["callsign"], "TEST1")
        self.assertEqual(snapshot["written_at"], turn["written_at"])

    def test_concurrent_append_survives_successful_inflight_write(self):
        self.ingestor.intervals.append(interval("inflight"))
        entered, release = threading.Event(), threading.Event()

        def block(table, rows):
            if table == "capture_intervals":
                entered.set()
                release.wait(2)

        self.spool.put.side_effect = block
        writer = threading.Thread(target=self.ingestor.persist)
        writer.start()
        self.assertTrue(entered.wait(1))
        self.ingestor.accept([self.event("new", "interval", interval("new"))])
        release.set()
        writer.join(2)
        self.assertFalse(writer.is_alive())
        self.assertEqual(list(self.ingestor.intervals), [interval("new")])

    def test_failed_write_restores_inflight_ahead_of_concurrent_appends(self):
        self.ingestor.intervals.append(interval("inflight"))
        entered, release = threading.Event(), threading.Event()

        def block_and_fail(table, rows):
            if table == "capture_intervals":
                entered.set()
                release.wait(2)
                raise OSError("disk full")

        self.spool.put.side_effect = block_and_fail
        writer = threading.Thread(target=self.ingestor.persist)
        writer.start()
        self.assertTrue(entered.wait(1))
        self.ingestor.accept([self.event("new", "interval", interval("new"))])
        release.set()
        writer.join(2)
        self.assertEqual(list(self.ingestor.intervals), [interval("inflight"), interval("new")])

    def test_snapshot_failure_restores_snapshots_and_untouched_intervals(self):
        self.ingestor.accumulator.add_observation(observation())
        self.ingestor.intervals.append(interval("pending"))
        self.spool.put.side_effect = OSError("disk full")
        self.ingestor.persist(final=True)
        self.assertEqual(len(self.ingestor.snapshots), 1)
        self.assertEqual(list(self.ingestor.intervals), [interval("pending")])
        self.assertEqual([c.args[0] for c in self.spool.put.call_args_list], ["aircraft_snapshots"])

    def test_buffer_restore_keeps_oldest_work_within_capacity(self):
        self.ingestor.interval_limit = 2
        self.ingestor.intervals.extend([interval("new1"), interval("new2")])
        self.ingestor._restore_left(self.ingestor.intervals, [interval("old")], 2, "interval")
        self.assertEqual([i["decoder_version"] for i in self.ingestor.intervals], ["old", "new1"])

    def test_invalid_field_timestamp_rejected_without_seen_mutation(self):
        bad = observation()
        bad["fields"] = {"altitude": {"value": 2, "timestamp": 2}}
        with self.assertRaisesRegex(ValueError, "field timestamp"):
            self.ingestor.accept([self.event("bad-time", data=bad)])
        self.assertEqual(self.ingestor.seen, {})

    def test_late_grace_defaults_to_five_seconds(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(Ingestor(self.spool).late_grace, 5.0)

    def test_late_grace_reads_env_override(self):
        with patch.dict(os.environ, {"INGEST_LATE_GRACE_SECONDS": "12.5"}, clear=False):
            self.assertEqual(Ingestor(self.spool).late_grace, 12.5)

    def test_persist_flushes_using_configured_late_grace(self):
        self.ingestor.late_grace = 12.5
        with (
            patch("sky_scanner.ingest.time.time", return_value=1000.0),
            patch.object(self.ingestor.accumulator, "flush_ready", return_value=[]) as flush_ready,
        ):
            self.ingestor.persist()
        flush_ready.assert_called_once_with(987.5)


class HeardTrackingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "heard.json"
        self.spool = MagicMock()

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshots_update_heard_and_final_persist_saves_it(self):
        ingestor = Ingestor(self.spool, Heard(self.path))
        now = round(time.time())  # older callsigns would be pruned on save
        ingestor.accumulator.add_observation(observation(t=now))
        ingestor.persist(final=True)
        saved = Heard.load(self.path)
        self.assertEqual(list(saved.aircraft), [("icao", "ABC123")])
        self.assertEqual(saved.aircraft[("icao", "ABC123")].callsigns, {"TEST1": now})

    def test_heard_is_saved_at_most_once_per_interval(self):
        heard = MagicMock()
        ingestor = Ingestor(self.spool, heard, heard_save_seconds=60)
        ingestor.persist()
        heard.save.assert_not_called()
        ingestor._heard_saved_at -= 61
        ingestor.persist()
        heard.save.assert_called_once()

    def test_unspooled_snapshots_do_not_count_as_heard(self):
        heard = MagicMock()
        self.spool.put.side_effect = OSError("disk full")
        ingestor = Ingestor(self.spool, heard)
        ingestor.accumulator.add_observation(observation())
        ingestor.persist(final=True)
        heard.update.assert_not_called()

    def test_unreadable_heard_file_is_set_aside(self):
        self.path.write_text("{not json")
        with self.assertLogs("sky_scanner.ingest", "ERROR"):
            heard = load_heard(self.path)
        self.assertEqual(heard.aircraft, {})
        self.assertFalse(heard.seeded)
        self.assertTrue(self.path.with_name("heard.json.bad").exists())


class RowTests(unittest.TestCase):
    def snapshot(self, **fields):
        return {
            "bucket_start": 1_760_875_200,
            "receiver_id": "rx1",
            "address_type": "icao",
            "address": "ABC123",
            "frequency_hz": 1_090_000_000,
            "first_received_at": 1_760_875_201.5,
            "last_received_at": 1_760_875_214.25,
            "message_count": 7,
            "fields": {name: {"value": value, "timestamp": 1_760_875_201.5} for name, value in fields.items()},
        }

    def test_snapshot_row_matches_the_table_schema(self):
        row = snapshot_row(self.snapshot(callsign="RCH123 ", altitude_baro_ft=35000), WRITTEN)
        self.assertEqual(set(row), set(SPECS["aircraft_snapshots"].column_names) - {"_replicated_at"})
        self.assertEqual(row["bucket_start"], "2025-10-19T12:00:00+00:00")
        self.assertEqual(row["last_received_at"], "2025-10-19T12:00:14.250000+00:00")
        self.assertEqual((row["callsign"], row["altitude_baro_ft"], row["latitude"]), ("RCH123 ", 35000.0, None))
        self.assertEqual(row["fields"]["callsign"]["value"], "RCH123 ")
        self.assertEqual(row["written_at"], WRITTEN)

    def test_wrong_typed_values_become_null_instead_of_failing_the_batch(self):
        row = snapshot_row(
            self.snapshot(altitude_baro_ft="high", ground_speed_kt=float("nan"), on_ground="yes", squawk=7700),
            WRITTEN,
        )
        self.assertEqual((row["altitude_baro_ft"], row["ground_speed_kt"], row["on_ground"]), (None, None, None))
        self.assertEqual(row["squawk"], "7700")
        self.assertEqual(row["fields"]["altitude_baro_ft"]["value"], "high")

    def test_snapshot_rows_survive_the_spool_unchanged(self):
        row = snapshot_row(self.snapshot(callsign="X1", on_ground=False), WRITTEN)
        self.assertEqual(json.loads(json.dumps(row)), row)

    def test_interval_row(self):
        row = interval_row({**interval("v1"), "id": "abc", "ended_at": None}, WRITTEN)
        self.assertEqual(set(row), set(SPECS["capture_intervals"].column_names) - {"_replicated_at"})
        self.assertEqual((row["id"], row["ended_at"], row["sample_config"]), ("abc", None, {}))
        self.assertEqual(row["started_at"], "1970-01-01T00:00:01+00:00")

    def test_interval_without_id_gets_one(self):
        self.assertTrue(interval_row(interval("v1"), WRITTEN)["id"])


class SenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.spool = Spool(Path(self.tmp.name) / "spool")
        self.bigquery = MagicMock()
        self.now = [0.0]

    def tearDown(self):
        self.tmp.cleanup()

    def sender(self, heard=None, **kwargs):
        return Sender(self.spool, self.bigquery, heard, clock=lambda: self.now[0], **kwargs)

    def test_checks_tables_once_then_streams_spooled_rows(self):
        self.spool.put("aircraft_snapshots", [{"n": 1}])
        sender = self.sender()
        self.assertEqual(sender.step(), 1)
        self.assertEqual(
            [c.args[0].name for c in self.bigquery.ensure_table.call_args_list],
            ["aircraft_snapshots", "capture_intervals"],
        )
        self.bigquery.stream.assert_called_once_with(SPECS["aircraft_snapshots"], [{"n": 1}])
        self.assertEqual(self.spool.pending(), [])
        sender.step()
        self.assertEqual(self.bigquery.ensure_table.call_count, 2)

    def test_backs_off_while_bigquery_is_unreachable_and_keeps_the_rows(self):
        self.spool.put("aircraft_snapshots", [{"n": 1}])
        self.bigquery.stream.side_effect = ConnectionError("offline")
        sender = self.sender(retry_seconds=1, max_retry_seconds=4)
        with self.assertLogs("sky_scanner.ingest", "WARNING"):
            delays = [sender.step() for _ in range(4)]
        self.assertEqual(delays, [1, 2, 4, 4])
        self.assertEqual(len(self.spool.pending()), 1)
        self.bigquery.stream.side_effect = None
        self.assertEqual(sender.step(), 1)
        self.assertEqual(sender.delay, 1)
        self.assertEqual(self.spool.pending(), [])

    def test_a_rejected_batch_does_not_stop_the_others(self):
        self.spool.put("aircraft_snapshots", [{"n": 1}])
        self.spool.put("aircraft_snapshots", [{"n": 2}])
        self.bigquery.stream.side_effect = [RowsRejected("bad"), None]
        with self.assertLogs("sky_scanner.spool", "ERROR"):
            self.sender().step()
        self.assertEqual(self.spool.pending(), [])

    def test_more_waiting_means_no_pause(self):
        for n in range(60):
            self.spool.put("aircraft_snapshots", [{"n": n}])
        self.assertEqual(self.sender().step(), 0)

    def test_seeds_heard_once(self):
        heard = Heard(Path(self.tmp.name) / "heard.json")
        self.bigquery.query.return_value = [
            {"address_type": "icao", "address": "ae1234", "first_seen": 1.0, "last_seen": 2.0, "callsigns": []}
        ]
        sender = self.sender(heard)
        sender.step()
        sender.step()
        self.bigquery.query.assert_called_once()
        self.assertTrue(heard.seeded)
        self.assertIn(("icao", "AE1234"), heard.aircraft)

    def test_failed_seeding_is_retried_later_without_blocking_sends(self):
        heard = Heard(Path(self.tmp.name) / "heard.json")
        self.bigquery.query.side_effect = ConnectionError("offline")
        self.spool.put("aircraft_snapshots", [{"n": 1}])
        sender = self.sender(heard, seed_retry_seconds=300)
        with self.assertLogs("sky_scanner.ingest", "WARNING"):
            sender.step()
        self.assertEqual(self.spool.pending(), [])
        sender.step()
        self.assertEqual(self.bigquery.query.call_count, 1)
        self.now[0] = 301
        self.bigquery.query.side_effect = None
        self.bigquery.query.return_value = []
        sender.step()
        self.assertTrue(heard.seeded)


if __name__ == "__main__":
    unittest.main()
