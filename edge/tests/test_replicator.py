import threading
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from sky_scanner.replicator import (
    REPLICATED_AT,
    Replicator,
    TableSpec,
    bq_type,
    run,
    to_json_value,
)

T0 = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
SNAPSHOTS = TableSpec("aircraft_snapshots", "stream", key=("address",), partition="bucket_start")
RECEIVERS = TableSpec("receivers", "reload", exclude=frozenset({"location"}))

CATALOG = {
    "aircraft_snapshots": [
        ("address", "text", False, False),
        ("bucket_start", "timestamp with time zone", False, False),
        ("fields", "jsonb", False, False),
        ("position", "USER-DEFINED", True, True),
        ("written_at", "timestamp with time zone", False, False),
    ],
    "receivers": [
        ("receiver_id", "text", False, False),
        ("location", "USER-DEFINED", True, False),
    ],
}


class FakeSource:
    def __init__(self):
        self.tables = {"aircraft_snapshots": [], "receivers": [{"receiver_id": "rx"}]}
        self.current = T0

    def columns(self, table):
        return CATALOG[table]

    def now(self):
        return self.current

    def rows(self, table, columns, after=None, until=None):
        for row in self.tables[table]:
            if after is not None and row["written_at"] <= after:
                continue
            if until is not None and row["written_at"] > until:
                continue
            yield {c: row[c] for c in columns}

    def write(self, address, written_at):
        self.tables["aircraft_snapshots"].append(
            {"address": address, "bucket_start": written_at, "fields": {"a": 1},
             "written_at": written_at})


class FakeSink:
    def __init__(self, max_written_at=None):
        self.created, self.loads, self.streamed = {}, [], []
        self._max = max_written_at

    def ensure_table(self, spec, schema):
        self.created[spec.name] = schema

    def max_written_at(self, table):
        return self._max

    def load(self, spec, schema, rows, truncate):
        rows = list(rows)
        self.loads.append((spec.name, rows, truncate))
        return len(rows)

    def stream(self, table, rows, row_ids):
        self.streamed.append((table, rows, row_ids))

    def load_csv(self, table, data):
        self.loads.append((table, data, True))


def replicator(source, sink, **kwargs):
    kwargs.setdefault("urls", {})
    r = Replicator(source, sink, tables=(SNAPSHOTS, RECEIVERS), clock=lambda: T0, **kwargs)
    r.prepare()
    return r


class TypeTests(unittest.TestCase):
    def test_maps_postgres_types(self):
        self.assertEqual(bq_type("timestamp with time zone"), "TIMESTAMP")
        self.assertEqual(bq_type("jsonb"), "JSON")
        self.assertEqual(bq_type("uuid"), "STRING")

    def test_rejects_unknown_types(self):
        with self.assertRaises(ValueError):
            bq_type("USER-DEFINED")

    def test_converts_values_for_json_requests(self):
        self.assertEqual(to_json_value(T0), "2026-10-15T12:00:00+00:00")
        self.assertEqual(to_json_value(UUID(int=1)), "00000000-0000-0000-0000-000000000001")
        self.assertEqual(to_json_value(Decimal("1.5")), "1.5")
        self.assertEqual(to_json_value({"a": 1}), {"a": 1})
        self.assertEqual(to_json_value({"a": 1}, json_as_text=True), '{"a":1}')


class SchemaTests(unittest.TestCase):
    def test_skips_generated_and_excluded_columns_and_adds_replicated_at(self):
        sink = FakeSink()
        replicator(FakeSource(), sink)
        self.assertEqual([c.name for c in sink.created["aircraft_snapshots"]],
                         ["address", "bucket_start", "fields", "written_at", REPLICATED_AT])
        self.assertEqual([c.name for c in sink.created["receivers"]],
                         ["receiver_id", REPLICATED_AT])


class StreamTests(unittest.TestCase):
    def test_backfills_an_empty_table_up_to_the_cutoff(self):
        source, sink = FakeSource(), FakeSink(max_written_at=None)
        source.write("A", T0 - timedelta(hours=1))
        source.write("B", T0 + timedelta(seconds=5))  # after the cutoff
        r = replicator(source, sink)
        r.start_stream(SNAPSHOTS)
        (name, rows, truncate), = sink.loads
        self.assertEqual((name, [x["address"] for x in rows], truncate),
                         ("aircraft_snapshots", ["A"], False))
        self.assertEqual(r.streams["aircraft_snapshots"].watermark, T0)

    def test_does_not_restream_backfilled_rows_inside_the_overlap(self):
        source, sink = FakeSource(), FakeSink(max_written_at=None)
        # A schema migration stamps every existing row with the same time,
        # just before the backfill cutoff.
        for address in ("A", "B", "C"):
            source.write(address, T0 - timedelta(seconds=5))
        r = replicator(source, sink)
        r.start_stream(SNAPSHOTS)
        source.write("NEW", T0 + timedelta(seconds=10))
        self.assertEqual(r.stream_once(SNAPSHOTS), 1)
        self.assertEqual(sink.streamed[0][1][0]["address"], "NEW")

    def test_streams_new_rows_once_despite_the_overlap(self):
        source, sink = FakeSource(), FakeSink(max_written_at=T0)
        r = replicator(source, sink)
        r.start_stream(SNAPSHOTS)
        source.write("A", T0 + timedelta(seconds=10))
        self.assertEqual(r.stream_once(SNAPSHOTS), 1)
        source.write("B", T0 + timedelta(seconds=40))
        self.assertEqual(r.stream_once(SNAPSHOTS), 1)
        self.assertEqual([[row["address"] for row in rows] for _, rows, _ in sink.streamed],
                         [["A"], ["B"]])

    def test_finds_a_row_committed_late_inside_the_overlap(self):
        source, sink = FakeSource(), FakeSink(max_written_at=T0)
        r = replicator(source, sink)
        r.start_stream(SNAPSHOTS)
        source.write("A", T0 + timedelta(seconds=60))
        r.stream_once(SNAPSHOTS)
        # Written (stamped) before A, but committed after the previous poll.
        source.write("LATE", T0 + timedelta(seconds=30))
        self.assertEqual(r.stream_once(SNAPSHOTS), 1)
        self.assertEqual(sink.streamed[-1][1][0]["address"], "LATE")

    def test_json_columns_are_sent_as_text_when_streaming(self):
        source, sink = FakeSource(), FakeSink(max_written_at=T0)
        r = replicator(source, sink)
        r.start_stream(SNAPSHOTS)
        source.write("A", T0 + timedelta(seconds=1))
        r.stream_once(SNAPSHOTS)
        row = sink.streamed[0][1][0]
        self.assertEqual(row["fields"], '{"a":1}')
        self.assertEqual(row["written_at"], "2026-10-15T12:00:01+00:00")

    def test_does_not_advance_when_streaming_fails(self):
        source, sink = FakeSource(), FakeSink(max_written_at=T0)
        r = replicator(source, sink)
        r.start_stream(SNAPSHOTS)
        source.write("A", T0 + timedelta(seconds=1))

        def fail(*_):
            raise RuntimeError("rejected")

        sink.stream = fail
        with self.assertRaises(RuntimeError):
            r.stream_once(SNAPSHOTS)
        self.assertEqual(r.streams["aircraft_snapshots"].watermark, T0)
        self.assertEqual(r.streams["aircraft_snapshots"].sent, {})


class ReloadTests(unittest.TestCase):
    def test_reload_truncates_and_omits_excluded_columns(self):
        source, sink = FakeSource(), FakeSink()
        r = replicator(source, sink)
        r.reload(RECEIVERS)
        (name, rows, truncate), = sink.loads
        self.assertTrue(truncate)
        self.assertEqual(rows, [{"receiver_id": "rx", REPLICATED_AT: T0}])


class RunLoopTests(unittest.TestCase):
    def run_polls(self, sink, polls, max_failures=5):
        source, stop = FakeSource(), threading.Event()
        source.write("A", T0 + timedelta(seconds=1))
        calls = {"n": 0}
        stream = sink.stream

        def counting_stream(*args):
            calls["n"] += 1
            if calls["n"] >= polls:
                stop.set()
            stream(*args)

        sink.stream = counting_stream
        r = Replicator(source, sink, tables=(SNAPSHOTS, RECEIVERS), clock=lambda: T0,
                       urls={"doc8643_types": "https://example.test/types.csv"},
                       fetch=lambda url: b"Designator\nB738\n")
        run(r, stop, interval=0, reload_every=timedelta(hours=24), max_failures=max_failures)
        return sink

    def test_a_failing_table_is_retried_without_reloading_the_others(self):
        sink = FakeSink(max_written_at=T0)
        failures = {"left": 2}
        real = sink.stream

        def flaky(*args):
            if failures["left"]:
                failures["left"] -= 1
                raise RuntimeError("table not found")
            real(*args)

        sink.stream = flaky
        self.run_polls(sink, polls=3)
        self.assertEqual(len(sink.streamed), 1)
        self.assertEqual([name for name, _, _ in sink.loads], ["receivers", "doc8643_types"])

    def test_gives_up_after_repeated_failures(self):
        sink = FakeSink(max_written_at=T0)

        def broken(*args):
            raise RuntimeError("down")

        sink.stream = broken
        with self.assertRaises(RuntimeError):
            self.run_polls(sink, polls=100, max_failures=3)


if __name__ == "__main__":
    unittest.main()
