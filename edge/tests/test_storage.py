import json
import os
import sys
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from sky_scanner.storage import (
    PostgresStorage,
    _METADATA_FILL_ADDED_FIELDS,
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

    def test_partial_import_skips_absent_insert(self):
        self.cursor.fetchone.return_value = None
        records = [{
            "icao_address": "ABC123", "registration": "N1",
            "source_url": "https://example.test/data",
            "retrieved_at": "2026-01-01T00:00:00Z", "dataset_hash": "h",
        }]
        self.storage.save_metadata_import(records, batch_size=1, partial=True)
        executed_sql = [c.args[0] for c in self.cursor.execute.call_args_list]
        self.assertIn(_METADATA_INSERT_CHANGED, executed_sql)
        self.assertNotIn(_METADATA_INSERT_ABSENT, executed_sql)
        self.connection.commit.assert_called_once()

    def test_default_partial_false_runs_absent_insert(self):
        self.cursor.fetchone.return_value = None
        records = [{
            "icao_address": "ABC123", "registration": "N1",
            "source_url": "https://example.test/data",
            "retrieved_at": "2026-01-01T00:00:00Z", "dataset_hash": "h",
        }]
        self.storage.save_metadata_import(records, batch_size=1)
        executed_sql = [c.args[0] for c in self.cursor.execute.call_args_list]
        self.assertIn(_METADATA_INSERT_CHANGED, executed_sql)
        self.assertIn(_METADATA_INSERT_ABSENT, executed_sql)

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

    def test_metadata_import_uses_default_source_name_when_omitted(self):
        self.cursor.fetchone.return_value = None
        records = [{
            "icao_address": "ABC123", "registration": "N1",
            "manufacturer": "ACME", "model": "1", "aircraft_type": "4",
            "registered_owner": "Owner", "operator": None,
            "icao_type_code": "B77W", "operator_icao": "KAL", "country": "US",
            "registrant_type": "3", "year_mfr": 2004,
            "source_url": "https://example.test/data", "retrieved_at": "2026-01-01T00:00:00Z",
            "dataset_hash": "sha256",
        }]
        with patch("sky_scanner.storage.uuid.uuid4", return_value="import-id"):
            self.storage.save_metadata_import(records, batch_size=1)
        stage_rows = self.cursor.executemany.call_args_list[0].args[1]
        self.assertEqual(len(stage_rows[0]), 15)
        self.assertEqual(stage_rows[0], (
            "icao", "ABC123", "N1", "ACME", "1", "4", "Owner", None,
            "B77W", "KAL", "US", None, None, "3", 2004,
        ))
        insert_import_params = self.cursor.execute.call_args_list[0].args[1]
        self.assertEqual(insert_import_params[-1], "faa_registry")
        calls_by_sql = {c.args[0]: c.args[1] for c in self.cursor.execute.call_args_list
                        if len(c.args) > 1 and isinstance(c.args[1], dict)}
        self.assertEqual(calls_by_sql[_METADATA_INSERT_CHANGED]["source_name"], "faa_registry")
        self.assertEqual(calls_by_sql[_METADATA_INSERT_ABSENT]["source_name"], "faa_registry")

    def test_metadata_import_passes_source_name_to_sql_and_params(self):
        self.cursor.fetchone.return_value = None
        records = [{
            "icao_address": "abc123", "registration": None,
            "manufacturer": None, "model": None, "aircraft_type": None,
            "registered_owner": "Korean Air", "operator": "Korean Air",
            "icao_type_code": "B77W", "operator_icao": "KAL", "country": "Republic of Korea",
            "source_url": "https://example.test/opensky.csv", "retrieved_at": "2026-01-01T00:00:00Z",
            "dataset_hash": "sha256-opensky",
        }]
        self.storage.save_metadata_import(records, batch_size=1, source_name="opensky")
        insert_import_params = self.cursor.execute.call_args_list[0].args[1]
        self.assertEqual(insert_import_params[-1], "opensky")
        stage_rows = self.cursor.executemany.call_args_list[0].args[1]
        self.assertEqual(stage_rows[0], (
            "icao", "abc123", None, None, None, None, "Korean Air", "Korean Air",
            "B77W", "KAL", "Republic of Korea", None, None, None, None,
        ))
        calls_by_sql = {c.args[0]: c.args[1] for c in self.cursor.execute.call_args_list
                        if len(c.args) > 1 and isinstance(c.args[1], dict)}
        self.assertEqual(calls_by_sql[_METADATA_INSERT_CHANGED]["source_name"], "opensky")
        self.assertEqual(calls_by_sql[_METADATA_INSERT_ABSENT]["source_name"], "opensky")
        self.assertIn("m.source_name = %(source_name)s", _METADATA_INSERT_CHANGED)
        self.assertIn("WHERE source_name = %(source_name)s", _METADATA_INSERT_ABSENT)

    def test_added_fields_are_filled_in_place_before_change_detection(self):
        self.cursor.fetchone.return_value = None
        records = [{"icao_address": "ABC123", "registration": "N1", "registrant_type": "1",
                    "year_mfr": 1975, "source_url": "u", "retrieved_at": "2026-01-01T00:00:00Z",
                    "dataset_hash": "h"}]
        self.storage.save_metadata_import(records, batch_size=1)
        sqls = [c.args[0] for c in self.cursor.execute.call_args_list]
        self.assertLess(sqls.index(_METADATA_FILL_ADDED_FIELDS),
                        sqls.index(_METADATA_INSERT_CHANGED))
        # Only unknown values are filled, and only on each aircraft's latest version.
        self.assertIn("m.registrant_type IS NULL AND m.year_mfr IS NULL",
                      _METADATA_FILL_ADDED_FIELDS)
        self.assertIn("ORDER BY latest.retrieved_at DESC, latest.id DESC LIMIT 1",
                      _METADATA_FILL_ADDED_FIELDS)
        # A null added field on the latest version is unknown, not a change.
        self.assertIn("latest.registrant_type IS NOT NULL", _METADATA_INSERT_CHANGED)
        self.assertIn("latest.year_mfr IS NOT NULL", _METADATA_INSERT_CHANGED)

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

    def test_replace_military_operator_codes_rejects_empty_list_without_touching_cursor(self):
        with self.assertRaisesRegex(ValueError, "empty"):
            self.storage.replace_military_operator_codes(
                [], source_url="https://example.test/ops.csv",
                retrieved_at=datetime(2026, 1, 1, tzinfo=timezone.utc), dataset_hash="h")
        self.connection.cursor.assert_not_called()

    def test_replace_military_operator_codes_deletes_then_inserts_and_commits(self):
        self.cursor.fetchone.return_value = None
        codes = [
            {"code": "RCH", "name": "Air Mobility Command"},
            {"code": "CNV", "name": "US Navy"},
        ]
        with patch("sky_scanner.storage.uuid.uuid4", return_value="import-id"):
            result = self.storage.replace_military_operator_codes(
                codes, source_url="https://example.test/ops.csv",
                retrieved_at=datetime(2026, 1, 1, tzinfo=timezone.utc), dataset_hash="h")
        self.assertEqual(result, "import-id")
        executed_sql = [c.args[0] for c in self.cursor.execute.call_args_list]
        self.assertTrue(any("'military_operator_codes'" in sql for sql in executed_sql))
        self.assertTrue(any(sql.strip().startswith("DELETE FROM military_operator_codes")
                            for sql in executed_sql))
        insert_sql, insert_rows = self.cursor.executemany.call_args.args
        self.assertIn("INSERT INTO military_operator_codes", insert_sql)
        self.assertEqual(insert_rows, [
            ("RCH", "Air Mobility Command", "import-id"),
            ("CNV", "US Navy", "import-id"),
        ])
        self.connection.commit.assert_called_once()

    def test_replace_military_operator_codes_rolls_back_on_failure(self):
        self.cursor.execute.side_effect = RuntimeError("boom")
        with self.assertRaisesRegex(RuntimeError, "boom"):
            self.storage.replace_military_operator_codes(
                [{"code": "RCH", "name": "x"}], source_url="u",
                retrieved_at=datetime(2026, 1, 1, tzinfo=timezone.utc), dataset_hash="h")
        self.connection.rollback.assert_called_once()
        self.connection.commit.assert_not_called()

    def test_save_callword_lookup_encodes_status_and_data_as_json(self):
        fetched_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.storage.save_callword_lookup(
            "TALON", "found", "https://wwi/TALON", {"types": [{"name": "H60", "count": 27}]},
            fetched_at=fetched_at)
        sql, params = self.cursor.execute.call_args.args
        self.assertIn("INSERT INTO callword_lookups", sql)
        self.assertEqual(params[0], "TALON")
        self.assertEqual(params[1], "found")
        self.assertEqual(params[2], "https://wwi/TALON")
        self.assertEqual(json.loads(params[4]), {"types": [{"name": "H60", "count": 27}]})
        self.connection.commit.assert_called_once()

    def test_save_callword_lookup_none_data_is_null_param(self):
        self.storage.save_callword_lookup("TALON", "not_found", "https://wwi/TALON", None)
        sql, params = self.cursor.execute.call_args.args
        self.assertEqual(params[1], "not_found")
        self.assertIsNone(params[4])

    def test_save_type_image_found_result_sets_status_and_fields(self):
        fetched_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = {
            "title": "Boeing 737-800", "description": "Narrow-body airliner",
            "article_url": "https://en.wikipedia.org/wiki/Boeing_737_Next_Generation",
            "image_url": "https://thumb.wikimedia.org/wikipedia/commons/thumb/a/ae/X.jpg/330px-X.jpg",
            "file_url": "https://commons.wikimedia.org/wiki/File:X.jpg",
        }
        self.storage.save_type_image("B738", "Boeing 737-800", "designator_list", result,
                                     fetched_at=fetched_at)
        sql, params = self.cursor.execute.call_args.args
        self.assertIn("INSERT INTO aircraft_type_images", sql)
        self.assertIn("ON CONFLICT (designator)", sql)
        self.assertEqual(params[0], "B738")
        self.assertEqual(params[1], "Boeing 737-800")
        self.assertEqual(params[2], "found")
        self.assertEqual(params[3], "designator_list")
        self.assertEqual(params[4], "Boeing 737-800")
        self.assertEqual(params[5], "Narrow-body airliner")
        self.assertEqual(params[6], result["article_url"])
        self.assertEqual(params[7], result["image_url"])
        self.assertEqual(params[8], result["file_url"])
        self.connection.commit.assert_called_once()

    def test_save_type_image_none_result_sets_not_found_with_null_fields(self):
        self.storage.save_type_image("ZZZZ", "Unknown Model", "search", None)
        sql, params = self.cursor.execute.call_args.args
        self.assertEqual(params[0], "ZZZZ")
        self.assertEqual(params[1], "Unknown Model")
        self.assertEqual(params[2], "not_found")
        self.assertEqual(params[3], "search")
        self.assertIsNone(params[4])
        self.assertIsNone(params[5])
        self.assertIsNone(params[6])
        self.assertIsNone(params[7])
        self.assertIsNone(params[8])


if __name__ == "__main__":
    unittest.main()
