import json
import os
import sys
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from sky_scanner.storage import (
    PostgresStorage,
    _METADATA_INSERT_ABSENT,
    _METADATA_INSERT_CHANGED,
)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value
        self.cursor.rowcount = 2
        self.storage = PostgresStorage(self.connection)

    def snapshot(self, start=0):
        return {
            "bucket_start": start,
            "receiver_id": "rx1",
            "address_type": "icao",
            "address": "ABC123",
            "frequency_hz": 1_090_000_000,
            "first_received_at": start + 1,
            "last_received_at": start + 4,
            "message_count": 3,
            "fields": {
                "callsign": {"value": "TEST1", "timestamp": start + 4, "source": "adsb"},
                "latitude": {"value": 37.5, "timestamp": start + 4, "source": "adsb"},
                "longitude": {"value": -122.2, "timestamp": start + 4, "source": "adsb"},
                "altitude_baro_ft": {"value": 12000, "timestamp": start + 2, "source": "adsb"},
                "ground_speed_kt": {"value": 220, "timestamp": start + 3, "source": "adsb"},
            },
        }

    def test_snapshot_batch_uses_composite_idempotency_and_typed_json_fields(self):
        count = self.storage.save_snapshots([self.snapshot(), self.snapshot(15)])
        self.assertEqual(count, 2)
        sql, rows = self.cursor.executemany.call_args.args
        self.assertIn("ON CONFLICT (bucket_start, receiver_id, address_type, address, frequency_hz)", sql)
        self.assertIn("DO NOTHING", sql)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], datetime.fromtimestamp(0, timezone.utc))
        self.assertEqual(rows[0][8], "TEST1")
        self.assertEqual(rows[0][11], 12000)
        self.assertEqual(rows[0][13], 220)
        self.assertEqual(json.loads(rows[0][-1])["callsign"]["timestamp"], 4)
        self.connection.commit.assert_called_once()
        self.cursor.close.assert_called_once()

    def test_empty_batch_does_not_open_transaction(self):
        self.assertEqual(self.storage.save_snapshots([]), 0)
        self.connection.cursor.assert_not_called()

    def test_snapshot_failure_rolls_back(self):
        self.cursor.executemany.side_effect = RuntimeError("db unavailable")
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            self.storage.save_snapshots([self.snapshot()])
        self.connection.rollback.assert_called_once()
        self.connection.commit.assert_not_called()

    def test_receiver_and_capture_interval_are_parameterized(self):
        self.storage.register_receiver("rx1", "desk", latitude=37.5, longitude=-122.2)
        sql, params = self.cursor.execute.call_args.args
        self.assertIn("ST_MakePoint(%s, %s)", sql)
        self.assertEqual(params, ("rx1", "desk", -122.2, 37.5))
        self.storage.save_capture_interval({
            "receiver_id": "rx1", "frequency_hz": 978_000_000,
            "started_at": 100, "ended_at": 130, "decoder_version": "test",
            "sample_config": {"rate": 20_000_000},
        })
        sql, params = self.cursor.execute.call_args.args
        self.assertIn("INSERT INTO capture_intervals", sql)
        self.assertEqual(params[1:3], ("rx1", 978_000_000))
        self.assertEqual(json.loads(params[-1]), {"rate": 20_000_000})

    def test_metadata_import_bulk_batches_and_keeps_owner_separate(self):
        self.cursor.fetchone.return_value = None
        records = [{
            "icao_address": f"ABC{i:03X}", "registration": f"N{i}",
            "manufacturer": "ACME", "model": "1", "aircraft_type": "4",
            "registered_owner": "Owner", "operator": None,
            "source_url": "https://example.test/data", "retrieved_at": "2026-01-01T00:00:00Z",
            "dataset_hash": "sha256", 
        } for i in range(3)]
        with patch("sky_scanner.storage.uuid.uuid4", return_value="import-id"):
            result = self.storage.save_metadata_import(records, batch_size=2)
        self.assertEqual(result, "import-id")
        calls = self.cursor.executemany.call_args_list
        self.assertEqual([len(call.args[1]) for call in calls], [2, 1])
        meta_sql, meta_rows = calls[0].args
        self.assertIn("registered_owner, operator", meta_sql)
        self.assertEqual(meta_rows[0][6:8], ("Owner", None))
        self.assertEqual(meta_rows[0][1], "ABC000")
        # Stage table setup, both changed/absent inserts, and the changed_count
        # rollup all happen through cursor.execute, not executemany.
        executed_sql = [c.args[0] for c in self.cursor.execute.call_args_list]
        self.assertTrue(any("CREATE TEMP TABLE sky_metadata_stage" in sql for sql in executed_sql))
        self.assertTrue(any("CREATE INDEX ON sky_metadata_stage" in sql for sql in executed_sql))
        self.assertIn(_METADATA_INSERT_CHANGED, executed_sql)
        self.assertIn(_METADATA_INSERT_ABSENT, executed_sql)
        self.assertTrue(any("UPDATE enrichment_imports SET changed_count" in sql for sql in executed_sql))
        self.connection.commit.assert_called_once()

    def test_metadata_import_failure_rolls_back(self):
        records = [{
            "icao_address": "ABC123", "registration": "N1",
            "source_url": "https://example.test/data",
            "retrieved_at": "2026-01-01T00:00:00Z", "dataset_hash": "h",
        }]
        self.cursor.execute.side_effect = RuntimeError("boom")
        with self.assertRaisesRegex(RuntimeError, "boom"):
            self.storage.save_metadata_import(records, batch_size=1)
        self.connection.rollback.assert_called_once()
        self.connection.commit.assert_not_called()
        self.cursor.close.assert_called_once()

    def test_schema_has_historical_asof_metadata_and_postgis(self):
        path = os.path.join(os.path.dirname(__file__), "..", "sql", "schema.sql")
        with open(path, encoding="utf-8") as schema_file:
            schema = schema_file.read()
        self.assertIn("CREATE EXTENSION IF NOT EXISTS postgis", schema)
        self.assertIn("ON aircraft_snapshots USING gist (position)", schema)
        self.assertIn("metadata.retrieved_at <= s.bucket_start + interval '15 seconds'", schema)
        self.assertIn("CREATE OR REPLACE VIEW enriched_snapshots_current", schema)
        self.assertIn("UNIQUE (address_type, address, source_url, dataset_hash)", schema)
        self.assertIn("registry_status", schema)
        self.assertIn("CHECK (registry_status IN ('present', 'absent'))", schema)

    def test_from_env_accepts_compose_database_url_and_imports_psycopg_lazily(self):
        fake_psycopg = types.SimpleNamespace(connect=MagicMock(return_value=self.connection))
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test/db"}, clear=True):
            with patch.dict(sys.modules, {"psycopg": fake_psycopg}):
                storage = PostgresStorage.from_env()
        self.assertIs(storage.connection, self.connection)
        fake_psycopg.connect.assert_called_once_with("postgresql://test/db")

    def test_same_source_hash_reimport_does_not_create_new_metadata_version(self):
        records = [{
            "icao_address": "ABC123", "registration": "N12345",
            "source_url": "https://example.test/data", "retrieved_at": "2026-01-01T00:00:00Z",
            "dataset_hash": "same-hash",
        }]
        self.storage.save_metadata_import(records, batch_size=1)
        # The idempotency guard now lives on the changed/absent INSERT ... SELECT
        # statements, executed individually (not as part of the executemany
        # stage-table load), each keyed with a dict of shared params.
        calls_by_sql = {c.args[0]: c.args[1] for c in self.cursor.execute.call_args_list
                        if len(c.args) > 1 and isinstance(c.args[1], dict)}
        self.assertIn(_METADATA_INSERT_CHANGED, calls_by_sql)
        self.assertIn(_METADATA_INSERT_ABSENT, calls_by_sql)
        self.assertIn("ON CONFLICT (address_type, address, source_url, dataset_hash)",
                      _METADATA_INSERT_CHANGED)
        self.assertIn("DO NOTHING", _METADATA_INSERT_CHANGED)
        self.assertIn("ON CONFLICT (address_type, address, source_url, dataset_hash)",
                      _METADATA_INSERT_ABSENT)
        self.assertIn("DO NOTHING", _METADATA_INSERT_ABSENT)
        # Changed-only writes require the registry fields to actually differ...
        self.assertIn("IS DISTINCT FROM", _METADATA_INSERT_CHANGED)
        # ...while a dropped-from-registry aircraft is recorded as absent.
        self.assertIn("'absent'", _METADATA_INSERT_ABSENT)
        params = calls_by_sql[_METADATA_INSERT_CHANGED]
        self.assertEqual(params["source_url"], "https://example.test/data")
        self.assertEqual(params["dataset_hash"], "same-hash")
        self.assertEqual(calls_by_sql[_METADATA_INSERT_ABSENT], params)


if __name__ == "__main__":
    unittest.main()
