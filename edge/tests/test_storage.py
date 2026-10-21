import unittest
from datetime import UTC, datetime

from sky_scanner.bq import SPECS
from sky_scanner.storage import _METADATA_SCRIPT, _VALUE_COLUMNS, BigQueryStorage, stage_rows

RETRIEVED = "2026-10-19T12:00:00Z"


class FakeBigQuery:
    def __init__(self, query_rows=None):
        self.calls = []
        self.query_rows = query_rows if query_rows is not None else [{"import_id": "imp-1", "changed": 3}]

    def ensure_table(self, spec):
        self.calls.append(("ensure", spec.name))

    def load(self, spec, rows, *, truncate=False):
        rows = list(rows)
        self.calls.append(("load", spec.name, rows, truncate))
        return len(rows)

    def load_csv(self, table, data):
        self.calls.append(("load_csv", table, data))

    def query(self, sql, params=None):
        self.calls.append(("query", sql, params))
        return self.query_rows

    def stream(self, spec, rows, **_):
        self.calls.append(("stream", spec.name, rows))
        return len(rows)

    def without_ensure(self):
        return [c for c in self.calls if c[0] != "ensure"]


def record(address, **values):
    return {
        "icao_address": address,
        "source_url": "https://faa.test/reg.zip",
        "retrieved_at": RETRIEVED,
        "dataset_hash": "hash1",
        **values,
    }


class StageRowTests(unittest.TestCase):
    def test_one_row_per_address_with_typed_values(self):
        rows = stage_rows(
            [
                record("ABC123", registration="N1", year_mfr="1999", registered_owner="Owner"),
                record("ABC123", registration="N2"),
                {"address_type": "tisb_other", "address": "ABC123", "model": 737},
            ]
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["registration"], "N1")
        self.assertEqual((rows[0]["year_mfr"], rows[0]["registered_owner"]), (1999, "Owner"))
        self.assertEqual((rows[1]["address_type"], rows[1]["model"]), ("tisb_other", "737"))

    def test_unusable_year_becomes_null(self):
        self.assertIsNone(stage_rows([record("ABC123", year_mfr="unknown")])[0]["year_mfr"])


class MetadataImportTests(unittest.TestCase):
    def setUp(self):
        self.bq = FakeBigQuery()
        self.storage = BigQueryStorage(self.bq)

    def test_stages_then_runs_the_version_script_once(self):
        import_id = self.storage.save_metadata_import([record("ABC123"), record("DEF456")], source_name="faa_registry")
        self.assertEqual(import_id, "imp-1")
        (load, query) = self.bq.without_ensure()
        self.assertEqual(load[:2], ("load", "_stage_metadata_faa_registry"))
        self.assertTrue(load[3])  # replaces any leftover staging rows
        sql, params = query[1], query[2]
        self.assertIn("FROM {dataset}._stage_metadata_faa_registry AS s", sql)
        self.assertNotIn("{stage}", sql)
        self.assertEqual(
            params,
            {
                "source_name": "faa_registry",
                "source_url": "https://faa.test/reg.zip",
                "retrieved_at": datetime(2026, 10, 19, 12, tzinfo=UTC),
                "dataset_hash": "hash1",
                "record_count": 2,
                "partial": False,
            },
        )

    def test_partial_imports_say_so(self):
        self.storage.save_metadata_import([record("ABC123")], source_name="tar1090_db", partial=True)
        self.assertTrue(self.bq.without_ensure()[1][2]["partial"])

    def test_refuses_a_full_import_without_records(self):
        with self.assertRaisesRegex(ValueError, "every aircraft would become absent"):
            self.storage.save_metadata_import(
                [], source_url="u", retrieved_at=RETRIEVED, dataset_hash="h", source_name="faa_registry"
            )
        self.assertEqual(self.bq.without_ensure(), [])

    def test_requires_provenance(self):
        with self.assertRaises(ValueError):
            self.storage.save_metadata_import([{"icao_address": "ABC123"}])

    def test_rejects_unsafe_source_names(self):
        with self.assertRaises(ValueError):
            self.storage.save_metadata_import([record("ABC123")], source_name="x; drop table")

    def test_tables_are_checked_once(self):
        self.storage.save_metadata_import([record("ABC123")])
        self.storage.save_metadata_import([record("ABC123")])
        ensured = [c[1] for c in self.bq.calls if c[0] == "ensure"]
        self.assertEqual(
            ensured,
            [
                "aircraft_metadata",
                "enrichment_imports",
                "military_operator_codes",
                "callword_lookups",
                "aircraft_type_images",
                "enrichment_runs",
            ],
        )


class MetadataScriptTests(unittest.TestCase):
    """The rules of change detection, as written in the script (run live in the rehearsal)."""

    def test_every_value_column_is_compared(self):
        for column in _VALUE_COLUMNS:
            self.assertIn(f"l.{column} IS DISTINCT FROM s.{column}", _METADATA_SCRIPT)

    def test_fields_added_later_only_count_when_known(self):
        for column in ("registrant_type", "year_mfr"):
            self.assertIn(f"(l.{column} IS NOT NULL AND l.{column} IS DISTINCT FROM s.{column})", _METADATA_SCRIPT)

    def test_absent_versions_only_for_full_imports(self):
        self.assertIn("WHERE NOT @partial", _METADATA_SCRIPT)

    def test_writes_happen_in_one_transaction_and_staging_is_dropped(self):
        begin, commit = _METADATA_SCRIPT.index("BEGIN TRANSACTION"), _METADATA_SCRIPT.index("COMMIT TRANSACTION")
        self.assertLess(begin, _METADATA_SCRIPT.index("INSERT INTO {dataset}.aircraft_metadata"))
        self.assertLess(_METADATA_SCRIPT.index("MERGE {dataset}.enrichment_imports"), commit)
        self.assertLess(commit, _METADATA_SCRIPT.index("DROP TABLE {stage}"))

    def test_reuses_the_import_row_of_the_same_dataset(self):
        self.assertIn("WHERE source_url = @source_url AND dataset_hash = @dataset_hash", _METADATA_SCRIPT)
        self.assertIn("changed_count = COALESCE(i.changed_count, 0) + changed", _METADATA_SCRIPT)


class OtherWriteTests(unittest.TestCase):
    def setUp(self):
        self.bq = FakeBigQuery(query_rows=[{"n": 1}])
        self.storage = BigQueryStorage(self.bq)

    def test_import_exists_with_and_without_a_hash(self):
        self.assertTrue(self.storage.import_exists("u", "h"))
        self.assertTrue(self.storage.import_exists("u"))
        (_, with_hash, with_params), (_, without_hash, without_params) = self.bq.without_ensure()
        self.assertIn("dataset_hash = @dataset_hash", with_hash)
        self.assertEqual(with_params, {"source_url": "u", "dataset_hash": "h"})
        self.assertNotIn("dataset_hash", without_hash)
        self.assertEqual(without_params, {"source_url": "u"})

    def test_import_exists_is_false_for_no_rows(self):
        self.bq.query_rows = [{"n": 0}]
        self.assertFalse(self.storage.import_exists("u"))

    def test_operator_codes_are_replaced_before_the_import_is_recorded(self):
        import_id = self.storage.replace_military_operator_codes(
            [{"code": "RCH", "name": "Air Mobility Command"}], source_url="u", retrieved_at=RETRIEVED, dataset_hash="h"
        )
        (load, query) = self.bq.without_ensure()
        self.assertEqual(load[:2], ("load", "military_operator_codes"))
        self.assertEqual(load[2], [{"code": "RCH", "name": "Air Mobility Command", "import_id": import_id}])
        self.assertTrue(load[3])
        self.assertEqual(query[2]["import_id"], import_id)
        self.assertEqual(query[2]["source_name"], "military_operator_codes")

    def test_operator_codes_refuse_an_empty_set(self):
        with self.assertRaises(ValueError):
            self.storage.replace_military_operator_codes([], source_url="u", retrieved_at=RETRIEVED, dataset_hash="h")
        self.assertEqual(self.bq.calls, [])

    def test_callword_lookups_are_streamed(self):
        self.storage.save_callword_lookup("TALON", "found", "https://wwi.test/TALON", {"countries": ["US"]}, 0)
        ((_, table, (row,)),) = self.bq.without_ensure()
        self.assertEqual(table, "callword_lookups")
        self.assertEqual(row["fetched_at"], datetime(1970, 1, 1, tzinfo=UTC))
        self.assertEqual((row["word"], row["status"], row["data"]), ("TALON", "found", {"countries": ["US"]}))
        self.assertEqual(set(row) | {"_replicated_at"}, set(SPECS["callword_lookups"].column_names))

    def test_type_images_record_found_and_not_found(self):
        self.storage.save_type_image("B738", "Boeing 737-800", "designator_list", {"title": "Boeing 737", "x": 1})
        self.storage.save_type_image("ZZZZ", None, "search", None)
        (_, _, (found,)), (_, _, (missing,)) = self.bq.without_ensure()
        self.assertEqual((found["status"], found["title"]), ("found", "Boeing 737"))
        self.assertNotIn("x", found)
        self.assertEqual((missing["status"], missing["title"], missing["image_url"]), ("not_found", None, None))
        self.assertEqual(set(found) | {"_replicated_at"}, set(SPECS["aircraft_type_images"].column_names))

    def test_runs_are_streamed(self):
        now = datetime(2026, 10, 19, tzinfo=UTC)
        self.storage.record_run("callwords", now, now, "ok", {"looked_up": []}, None)
        ((_, table, (row,)),) = self.bq.without_ensure()
        self.assertEqual(table, "enrichment_runs")
        self.assertEqual(set(row) | {"_replicated_at"}, set(SPECS["enrichment_runs"].column_names))

    def test_reference_csv_is_loaded(self):
        self.storage.load_reference_csv("doc8643_types", b"Designator\n")
        self.assertEqual(self.bq.calls, [("load_csv", "doc8643_types", b"Designator\n")])


if __name__ == "__main__":
    unittest.main()
