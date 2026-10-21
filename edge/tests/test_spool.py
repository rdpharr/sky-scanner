import tempfile
import unittest
from pathlib import Path

from sky_scanner.bq import RowsRejected
from sky_scanner.spool import Spool


class Recorder:
    def __init__(self, fail_on=None, reject_on=None):
        self.sent, self.fail_on, self.reject_on = [], fail_on, reject_on

    def __call__(self, table, rows):
        if rows[0].get("n") == self.fail_on:
            raise ConnectionError("offline")
        if rows[0].get("n") == self.reject_on:
            raise RowsRejected("invalid row")
        self.sent.append((table, rows))


class SpoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_sends_batches_oldest_first_and_deletes_them(self):
        spool, send = Spool(self.dir), Recorder()
        spool.put("aircraft_snapshots", [{"n": 1}, {"n": 2}])
        spool.put("capture_intervals", [{"n": 3}])
        self.assertEqual(spool.drain(send), 3)
        self.assertEqual(send.sent, [("aircraft_snapshots", [{"n": 1}, {"n": 2}]), ("capture_intervals", [{"n": 3}])])
        self.assertEqual(spool.pending(), [])

    def test_empty_batches_write_nothing(self):
        spool = Spool(self.dir)
        self.assertIsNone(spool.put("aircraft_snapshots", []))
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_a_failed_send_keeps_that_batch_and_every_later_one(self):
        spool = Spool(self.dir)
        for n in (1, 2, 3):
            spool.put("aircraft_snapshots", [{"n": n}])
        with self.assertRaises(ConnectionError):
            spool.drain(Recorder(fail_on=2))
        self.assertEqual(len(spool.pending()), 2)
        send = Recorder()
        spool.drain(send)
        self.assertEqual([rows[0]["n"] for _, rows in send.sent], [2, 3])

    def test_batches_survive_a_restart(self):
        Spool(self.dir).put("aircraft_snapshots", [{"n": 1}])
        send = Recorder()
        Spool(self.dir).drain(send)
        self.assertEqual(send.sent, [("aircraft_snapshots", [{"n": 1}])])

    def test_rejected_batches_are_set_aside_without_blocking(self):
        spool = Spool(self.dir)
        for n in (1, 2):
            spool.put("aircraft_snapshots", [{"n": n}])
        send = Recorder(reject_on=1)
        with self.assertLogs("sky_scanner.spool", "ERROR"):
            self.assertEqual(spool.drain(send), 1)
        self.assertEqual([rows[0]["n"] for _, rows in send.sent], [2])
        self.assertEqual([p.suffix for p in self.dir.iterdir()], [".rejected"])
        self.assertEqual(spool.pending(), [])

    def test_unreadable_batches_are_set_aside(self):
        spool = Spool(self.dir)
        path = spool.put("aircraft_snapshots", [{"n": 1}])
        path.write_bytes(b"{not json\n")
        with self.assertLogs("sky_scanner.spool", "ERROR"):
            self.assertEqual(spool.drain(Recorder()), 0)
        self.assertTrue(path.with_name(path.name + ".rejected").exists())

    def test_partial_writes_from_a_crash_are_removed(self):
        (self.dir / "00000000000000000001-000001.aircraft_snapshots.jsonl.tmp").write_text('{"n":')
        spool = Spool(self.dir)
        self.assertEqual(list(self.dir.iterdir()), [])
        self.assertEqual(spool.pending(), [])

    def test_drops_the_oldest_batches_past_the_cap(self):
        spool = Spool(self.dir, max_bytes=50)  # two 22-byte batches fit
        with self.assertLogs("sky_scanner.spool", "ERROR") as logs:
            for n in range(5):
                spool.put("aircraft_snapshots", [{"n": n, "pad": "x" * 5}])
        self.assertLessEqual(spool.size(), 50)
        send = Recorder()
        spool.drain(send)
        self.assertEqual([rows[0]["n"] for _, rows in send.sent], [3, 4])
        self.assertIn("dropped", logs.output[0])

    def test_keeps_the_newest_batch_even_if_it_alone_is_over_the_cap(self):
        spool = Spool(self.dir, max_bytes=1)
        spool.put("aircraft_snapshots", [{"n": 1}])
        self.assertEqual(len(spool.pending()), 1)

    def test_max_batches_limits_one_drain(self):
        spool = Spool(self.dir)
        for n in (1, 2, 3):
            spool.put("aircraft_snapshots", [{"n": n}])
        self.assertEqual(spool.drain(Recorder(), max_batches=2), 2)
        self.assertEqual(len(spool.pending()), 1)

    def test_rejects_unsafe_table_names(self):
        with self.assertRaises(ValueError):
            Spool(self.dir).put("../etc", [{"n": 1}])


if __name__ == "__main__":
    unittest.main()
