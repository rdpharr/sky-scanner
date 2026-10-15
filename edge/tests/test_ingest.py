import os
import threading
import unittest
from unittest.mock import MagicMock, patch

from sky_scanner.ingest import Ingestor


def observation(t=1, address="ABC123"):
    return {
        "received_at": t, "receiver_id": "rx1", "address": address,
        "address_type": "icao", "frequency_hz": 1_090_000_000,
        "fields": {"callsign": "TEST1"},
    }


def interval(name):
    return {
        "receiver_id": "rx1", "frequency_hz": 1_090_000_000,
        "started_at": 1, "ended_at": 31, "decoder_version": name,
    }


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.ingestor = Ingestor()
        self.storage = MagicMock()
        self.ingestor.storage = self.storage

    def event(self, identity, kind="observation", data=None):
        return {"id": identity, "type": kind,
                "data": data if data is not None else observation()}

    def test_validates_entire_batch_before_mutating_state(self):
        with self.assertRaisesRegex(ValueError, "unknown event"):
            self.ingestor.accept([
                self.event("ok"), self.event("bad", kind="unknown")
            ])
        self.assertEqual(self.ingestor.seen, {})
        self.assertEqual(self.ingestor.accumulator._buckets, {})

    def test_deduplicates_events_and_preserves_late_drop_behavior(self):
        event = self.event("same")
        self.ingestor.accept([event, event])
        self.assertEqual(next(iter(self.ingestor.accumulator._buckets.values())).message_count, 1)
        self.ingestor.persist(final=True)
        self.assertEqual(self.storage.save_snapshots.call_count, 1)
        self.ingestor.accept([event])
        self.assertEqual(self.ingestor.accumulator._buckets, {})

    def test_concurrent_append_survives_successful_inflight_write(self):
        self.ingestor.intervals.append(interval("inflight"))
        entered, release = threading.Event(), threading.Event()

        def block(_row):
            entered.set()
            release.wait(2)

        self.storage.save_capture_interval.side_effect = block
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

        def block_and_fail(_row):
            entered.set()
            release.wait(2)
            raise RuntimeError("temporary outage")

        self.storage.save_capture_interval.side_effect = block_and_fail
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
        self.storage.save_snapshots.side_effect = RuntimeError("db down")
        self.ingestor.persist(final=True)
        self.assertEqual(len(self.ingestor.snapshots), 1)
        self.assertEqual(list(self.ingestor.intervals), [interval("pending")])
        self.storage.save_capture_interval.assert_not_called()

    def test_buffer_restore_keeps_oldest_work_within_capacity(self):
        self.ingestor.interval_limit = 2
        self.ingestor.intervals.extend([interval("new1"), interval("new2")])
        self.ingestor._restore_left(self.ingestor.intervals,
                                    [interval("old")], 2, "interval")
        self.assertEqual([i["decoder_version"] for i in self.ingestor.intervals],
                         ["old", "new1"])

    def test_invalid_field_timestamp_rejected_without_seen_mutation(self):
        bad = observation()
        bad["fields"] = {"altitude": {"value": 2, "timestamp": 2}}
        with self.assertRaisesRegex(ValueError, "field timestamp"):
            self.ingestor.accept([self.event("bad-time", data=bad)])
        self.assertEqual(self.ingestor.seen, {})

    def test_late_grace_defaults_to_five_seconds(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(Ingestor().late_grace, 5.0)

    def test_late_grace_reads_env_override(self):
        with patch.dict(os.environ, {"INGEST_LATE_GRACE_SECONDS": "12.5"}, clear=False):
            self.assertEqual(Ingestor().late_grace, 12.5)

    def test_persist_flushes_using_configured_late_grace(self):
        self.ingestor.late_grace = 12.5
        with patch("sky_scanner.ingest.time.time", return_value=1000.0), \
                patch.object(self.ingestor.accumulator, "flush_ready",
                             return_value=[]) as flush_ready:
            self.ingestor.persist()
        flush_ready.assert_called_once_with(987.5)


if __name__ == "__main__":
    unittest.main()
