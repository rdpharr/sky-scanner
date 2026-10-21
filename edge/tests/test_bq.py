import json
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import UUID

from sky_scanner import bq
from sky_scanner.bq import REPLICATED_AT, SPECS, BigQuery, RowsRejected, row_id, schema_changes, to_json_value

T0 = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
try:
    import google.cloud.bigquery  # noqa: F401

    HAS_BIGQUERY = True
except ImportError:
    HAS_BIGQUERY = False


def snapshot(address="ABC123", written_at="2026-10-19T12:00:00+00:00"):
    return {
        "bucket_start": "2026-10-19T11:59:45+00:00",
        "receiver_id": "rx",
        "address_type": "icao",
        "address": address,
        "frequency_hz": 1_090_000_000,
        "first_received_at": "2026-10-19T11:59:46+00:00",
        "last_received_at": "2026-10-19T11:59:59+00:00",
        "message_count": 3,
        "fields": {"callsign": {"value": "TEST1", "timestamp": 1.0}},
        "written_at": written_at,
    }


class SpecTests(unittest.TestCase):
    def test_keys_partitions_and_clusters_name_real_columns(self):
        for spec in bq.TABLES:
            names = spec.column_names
            for name in (*spec.key, *spec.cluster, *([spec.partition] if spec.partition else [])):
                self.assertIn(name, names, spec.name)

    def test_every_table_records_when_the_edge_sent_it(self):
        for spec in bq.TABLES:
            self.assertEqual(spec.columns[-1], bq.Column(REPLICATED_AT, "TIMESTAMP", False), spec.name)

    def test_column_shorthand(self):
        self.assertEqual(
            bq._columns("a STRING!", "b JSON"), (bq.Column("a", "STRING", False), bq.Column("b", "JSON", True))
        )


class ValueTests(unittest.TestCase):
    def test_converts_values_for_json_requests(self):
        self.assertEqual(to_json_value(T0), "2026-10-19T12:00:00+00:00")
        self.assertEqual(to_json_value(UUID(int=1)), "00000000-0000-0000-0000-000000000001")
        self.assertEqual(to_json_value(Decimal("1.5")), "1.5")
        self.assertEqual(to_json_value({"a": 1}), {"a": 1})
        self.assertEqual(to_json_value({"a": 1}, json_as_text=True), '{"a":1}')

    def test_json_columns_become_text_for_streaming(self):
        row = bq.json_row(SPECS["aircraft_snapshots"], snapshot())
        self.assertEqual(json.loads(row["fields"]), {"callsign": {"value": "TEST1", "timestamp": 1.0}})
        self.assertEqual(row["address"], "ABC123")


class RowIdTests(unittest.TestCase):
    spec = SPECS["aircraft_snapshots"]

    def test_is_stable_for_the_same_row(self):
        self.assertEqual(row_id(self.spec, snapshot()), row_id(self.spec, snapshot()))

    def test_differs_by_key_and_by_written_at(self):
        base = row_id(self.spec, snapshot())
        self.assertNotEqual(base, row_id(self.spec, snapshot(address="DEF456")))
        # A rewrite (e.g. an interval's end) is a new row, not a duplicate.
        self.assertNotEqual(base, row_id(self.spec, snapshot(written_at="2026-10-19T12:00:30+00:00")))

    def test_tables_without_written_at_use_the_key_alone(self):
        spec = SPECS["callword_lookups"]
        row = {"word": "TALON", "fetched_at": T0, "status": "found"}
        self.assertEqual(row_id(spec, row), row_id(spec, {**row, "status": "not_found"}))

    def test_requires_a_key(self):
        with self.assertRaises(ValueError):
            row_id(bq.TableSpec("keyless", bq._columns("a STRING")), {"a": "x"})


class SchemaChangeTests(unittest.TestCase):
    spec = bq.TableSpec("t", bq._columns("a STRING!", "b INT64", "c JSON"))

    def test_finds_missing_columns(self):
        added, mismatched = schema_changes(self.spec, [("a", "STRING"), ("b", "INT64")])
        self.assertEqual([c.name for c in added], ["c"])
        self.assertEqual(mismatched, [])

    def test_accepts_legacy_type_names(self):
        self.assertEqual(schema_changes(self.spec, [("a", "STRING"), ("b", "INTEGER"), ("c", "JSON")]), ([], []))

    def test_reports_type_differences(self):
        _, mismatched = schema_changes(self.spec, [("a", "STRING"), ("b", "STRING"), ("c", "JSON")])
        self.assertEqual(mismatched, ["b: STRING in BigQuery, INT64 expected"])


@unittest.skipUnless(HAS_BIGQUERY, "google-cloud-bigquery not installed (runs in the image)")
class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.client.project = "proj"
        self.client.insert_rows_json.return_value = []
        self.bq = BigQuery(self.client, "raw")

    def test_stream_stamps_rows_and_sends_insert_ids(self):
        spec = SPECS["aircraft_snapshots"]
        self.assertEqual(self.bq.stream(spec, [snapshot()], sent_at=T0), 1)
        (table, rows), kwargs = self.client.insert_rows_json.call_args
        self.assertEqual(table, "proj.raw.aircraft_snapshots")
        self.assertEqual(rows[0][REPLICATED_AT], "2026-10-19T12:00:00+00:00")
        self.assertIsInstance(rows[0]["fields"], str)
        self.assertEqual(kwargs["row_ids"], [row_id(spec, snapshot())])

    def test_stream_sends_in_batches(self):
        rows = [snapshot(address=f"{i:06X}") for i in range(bq.STREAM_BATCH_ROWS + 1)]
        self.bq.stream(SPECS["aircraft_snapshots"], rows)
        self.assertEqual([len(c.args[1]) for c in self.client.insert_rows_json.call_args_list], [500, 1])

    def test_stream_raises_rows_rejected_on_row_errors(self):
        self.client.insert_rows_json.return_value = [{"index": 0, "errors": [{"reason": "invalid"}]}]
        with self.assertRaises(RowsRejected):
            self.bq.stream(SPECS["aircraft_snapshots"], [snapshot()])

    def test_stream_lets_transport_errors_through(self):
        self.client.insert_rows_json.side_effect = ConnectionError("offline")
        with self.assertRaises(ConnectionError):
            self.bq.stream(SPECS["aircraft_snapshots"], [snapshot()])

    def test_query_caps_bytes_and_types_parameters(self):
        self.client.query.return_value.result.return_value = []
        sql = "select 1 from {dataset}.t where a = @a and b in unnest(@b) and t > @t"
        self.bq.query(sql, {"a": 1, "b": ["X", "Y"], "t": T0})
        (sql,) = self.client.query.call_args.args
        config = self.client.query.call_args.kwargs["job_config"]
        self.assertIn("`proj.raw`.t", sql)
        self.assertEqual(config.maximum_bytes_billed, bq.DEFAULT_MAX_BYTES_BILLED)
        kinds = {p.name: getattr(p, "type_", None) or p.array_type for p in config.query_parameters}
        self.assertEqual(kinds, {"a": "INT64", "b": "STRING", "t": "TIMESTAMP"})

    def test_ensure_table_creates_a_missing_table_with_partitioning(self):
        from google.api_core.exceptions import NotFound

        self.client.get_table.side_effect = NotFound("missing")
        self.bq.ensure_table(SPECS["enrichment_runs"])
        (table,), _ = self.client.create_table.call_args
        self.assertEqual(table.time_partitioning.field, "started_at")
        self.assertEqual([f.name for f in table.schema], list(SPECS["enrichment_runs"].column_names))

    def test_ensure_table_adds_missing_columns_as_nullable(self):
        from google.cloud import bigquery

        spec = bq.TableSpec("t", bq._columns("a STRING!", "b INT64!"))
        self.client.get_table.return_value = MagicMock(schema=[bigquery.SchemaField("a", "STRING", mode="REQUIRED")])
        self.bq.ensure_table(spec)
        table, fields = self.client.update_table.call_args.args
        self.assertEqual(fields, ["schema"])
        self.assertEqual([(f.name, f.mode) for f in table.schema], [("a", "REQUIRED"), ("b", "NULLABLE")])

    def test_ensure_table_refuses_a_type_change(self):
        from google.cloud import bigquery

        spec = bq.TableSpec("t", bq._columns("a INT64"))
        self.client.get_table.return_value = MagicMock(schema=[bigquery.SchemaField("a", "STRING")])
        with self.assertRaises(RuntimeError):
            self.bq.ensure_table(spec)
        self.client.update_table.assert_not_called()

    def test_load_appends_rows_with_the_send_time(self):
        import gzip

        loaded = {}

        def capture(file, table, job_config):
            loaded["lines"] = gzip.decompress(file.read()).splitlines()
            loaded["table"], loaded["config"] = table, job_config
            return MagicMock()

        self.client.load_table_from_file.side_effect = capture
        count = self.bq.load(SPECS["military_operator_codes"], [{"code": "RCH", "name": "Air Mobility Command"}])
        self.assertEqual(count, 1)
        self.assertEqual(loaded["table"], "proj.raw.military_operator_codes")
        self.assertEqual(loaded["config"].write_disposition, "WRITE_APPEND")
        self.assertIn(REPLICATED_AT, json.loads(loaded["lines"][0]))


if __name__ == "__main__":
    unittest.main()
