import csv
import hashlib
import io
import unittest
from datetime import UTC, datetime
from unittest.mock import MagicMock

from sky_scanner import opensky
from sky_scanner.enrich_worker import OpenSkyWorker, run_forever

S3_NS = "http://s3.amazonaws.com/doc/2006-03-01/"

OPENSKY_HEADER = [
    "icao24",
    "timestamp",
    "acars",
    "adsb",
    "built",
    "categoryDescription",
    "country",
    "engines",
    "firstFlightDate",
    "firstSeen",
    "icaoAircraftClass",
    "lineNumber",
    "manufacturerIcao",
    "manufacturerName",
    "model",
    "modes",
    "nextReg",
    "notes",
    "operator",
    "operatorCallsign",
    "operatorIata",
    "operatorIcao",
    "owner",
    "prevReg",
    "regUntil",
    "registered",
    "registration",
    "selCal",
    "serialNumber",
    "status",
    "typecode",
    "vdl",
]


def _s3_listing(keys):
    contents = "".join(f"<Contents><Key>{key}</Key></Contents>" for key in keys)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><ListBucketResult xmlns="{S3_NS}">{contents}</ListBucketResult>'
    return xml.encode("utf-8")


def _row(overrides: dict) -> list:
    row = {name: "" for name in OPENSKY_HEADER}
    row.update(overrides)
    return [row[name] for name in OPENSKY_HEADER]


def _csv_bytes(rows: list[list], header: list[str] | None = None, extra_raw_lines: list[str] | None = None) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, quotechar="'", quoting=csv.QUOTE_ALL)
    writer.writerow(header if header is not None else OPENSKY_HEADER)
    for row in rows:
        writer.writerow(row)
    text = buffer.getvalue()
    if extra_raw_lines:
        text += "".join(line + "\r\n" for line in extra_raw_lines)
    return text.encode("utf-8")


KOREAN_AIR_ROW = _row(
    {
        "icao24": "71c010",
        "timestamp": "2020-01-01 00:00:00",
        "country": "Republic of Korea",
        "icaoAircraftClass": "L2J",
        "manufacturerIcao": "BOEING",
        "manufacturerName": "Boeing",
        "model": "Boeing 777-3B5ER",
        "operator": "Korean Air",
        "operatorIcao": "KAL",
        "owner": "Korean Air",
        "registration": "HL8010",
        "typecode": "B77W",
    }
)


def valid_snapshot_bytes(n: int = 1) -> bytes:
    rows = []
    for i in range(n):
        rows.append(
            _row(
                {
                    "icao24": f"{i:06x}",
                    "registration": f"N{i}",
                    "operator": "Test Air",
                    "typecode": "B77W",
                }
            )
        )
    return _csv_bytes(rows)


class LatestSnapshotUrlTests(unittest.TestCase):
    def test_picks_newest_complete_snapshot_ignoring_partial_and_readme(self):
        listing = _s3_listing(
            [
                "metadata/aircraftDatabase-2024-01.csv",
                "metadata/README.txt",
                "metadata/aircraft-database-complete-2023-11.csv",
                "metadata/aircraft-database-complete-2024-02.csv",
                "metadata/aircraft-database-complete-2024-01.csv",
            ]
        )
        url = opensky.latest_snapshot_url(listing)
        self.assertEqual(url, opensky.BUCKET_URL + "/metadata/aircraft-database-complete-2024-02.csv")

    def test_no_matching_snapshot_raises(self):
        listing = _s3_listing(
            [
                "metadata/aircraftDatabase-2024-01.csv",
                "metadata/README.txt",
            ]
        )
        with self.assertRaises(ValueError):
            opensky.latest_snapshot_url(listing)

    def test_empty_listing_raises(self):
        with self.assertRaises(ValueError):
            opensky.latest_snapshot_url(_s3_listing([]))


class ParseOpenskyDatabaseTests(unittest.TestCase):
    def test_valid_row_is_parsed_with_expected_fields(self):
        data = _csv_bytes([KOREAN_AIR_ROW])
        retrieved = datetime(2026, 1, 1, tzinfo=UTC)
        records = opensky.parse_opensky_database(
            data, source_url="https://example.test/snapshot.csv", retrieved_at=retrieved
        )
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["icao_address"], "71C010")
        self.assertEqual(record["address_type"], "icao")
        self.assertEqual(record["registration"], "HL8010")
        self.assertEqual(record["manufacturer"], "Boeing")
        self.assertEqual(record["model"], "Boeing 777-3B5ER")
        self.assertEqual(record["aircraft_type"], "L2J")
        self.assertEqual(record["icao_type_code"], "B77W")
        self.assertEqual(record["registered_owner"], "Korean Air")
        self.assertEqual(record["operator"], "Korean Air")
        self.assertEqual(record["operator_icao"], "KAL")
        self.assertEqual(record["country"], "Republic of Korea")
        self.assertEqual(record["source_url"], "https://example.test/snapshot.csv")
        self.assertEqual(record["retrieved_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(record["dataset_hash"], hashlib.sha256(data).hexdigest())

    def test_empty_fields_become_none(self):
        row = _row({"icao24": "abc123", "operator": "Sole Operator"})
        data = _csv_bytes([row])
        records = opensky.parse_opensky_database(data, source_url="u")
        record = records[0]
        self.assertIsNone(record["registration"])
        self.assertIsNone(record["manufacturer"])
        self.assertIsNone(record["model"])
        self.assertIsNone(record["icao_type_code"])
        self.assertIsNone(record["registered_owner"])
        self.assertIsNone(record["country"])
        self.assertEqual(record["operator"], "Sole Operator")

    def test_row_with_only_address_and_no_useful_fields_is_dropped(self):
        row = _row({"icao24": "abc123", "manufacturerName": "Boeing"})
        data = _csv_bytes([row])
        records = opensky.parse_opensky_database(data, source_url="u")
        self.assertEqual(records, [])

    def test_row_with_invalid_icao_is_dropped(self):
        row = _row({"icao24": "not-hex-zz", "operator": "Some Op"})
        data = _csv_bytes([row])
        records = opensky.parse_opensky_database(data, source_url="u")
        self.assertEqual(records, [])

    def test_row_with_empty_icao_is_dropped(self):
        row = _row({"icao24": "", "operator": "Some Op"})
        data = _csv_bytes([row])
        records = opensky.parse_opensky_database(data, source_url="u")
        self.assertEqual(records, [])

    def test_duplicate_addresses_keep_first(self):
        first = _row({"icao24": "abc123", "operator": "First Operator"})
        second = _row({"icao24": "ABC123", "operator": "Second Operator"})
        data = _csv_bytes([first, second])
        records = opensky.parse_opensky_database(data, source_url="u")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["operator"], "First Operator")

    def test_wrong_column_count_rows_are_skipped(self):
        good = _row({"icao24": "abc123", "operator": "Good Op"})
        # A raw line with far fewer fields than the header.
        short_line = "'def456','2020-01-01',0,0"
        data = _csv_bytes([good], extra_raw_lines=[short_line])
        records = opensky.parse_opensky_database(data, source_url="u")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["icao_address"], "ABC123")

    def test_values_containing_commas_inside_quotes_parse_correctly(self):
        row = _row(
            {
                "icao24": "abc123",
                "operator": "Korean Air, Ltd",
                "owner": "Trust, LLC",
            }
        )
        data = _csv_bytes([row])
        records = opensky.parse_opensky_database(data, source_url="u")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["operator"], "Korean Air, Ltd")
        self.assertEqual(records[0]["registered_owner"], "Trust, LLC")

    def test_missing_required_column_raises(self):
        header = [name for name in OPENSKY_HEADER if name != "typecode"]
        row = ["" for _ in header]
        data = _csv_bytes([row], header=header)
        with self.assertRaises(ValueError):
            opensky.parse_opensky_database(data, source_url="u")


class Storage:
    def __init__(self, exists=False):
        self.exists = exists
        self.checked = []
        self.saved = []

    def import_exists(self, url, dataset_hash=None):
        self.checked.append(url)
        return self.exists

    def save_metadata_import(self, records, **kwargs):
        self.saved.append((records, kwargs))
        return "import-uuid"


class OpenSkyWorkerTests(unittest.TestCase):
    def test_already_imported_url_is_skipped_without_download(self):
        storage = Storage(exists=True)
        download = MagicMock()
        worker = OpenSkyWorker(
            storage=storage,
            find_latest=lambda: "https://example.test/snap.csv",
            download=download,
            min_records=1,
            emit=lambda _: None,
        )
        summary = worker.process_once()
        self.assertEqual(summary["status"], "skipped")
        self.assertEqual(summary["source"], "opensky")
        download.assert_not_called()
        self.assertEqual(storage.saved, [])

    def test_new_url_downloads_parses_and_saves_with_source_name(self):
        storage = Storage(exists=False)
        url = "https://example.test/snap.csv"
        data = valid_snapshot_bytes(3)
        download = MagicMock(return_value=data)
        worker = OpenSkyWorker(
            storage=storage,
            find_latest=lambda: url,
            download=download,
            min_records=1,
            emit=lambda _: None,
        )
        summary = worker.process_once()
        self.assertEqual(summary["status"], "imported")
        self.assertEqual(summary["source"], "opensky")
        download.assert_called_once_with(url)
        self.assertEqual(len(storage.saved), 1)
        records, kwargs = storage.saved[0]
        self.assertEqual(len(records), 3)
        self.assertEqual(kwargs["source_name"], "opensky")
        self.assertEqual(kwargs["source_url"], url)

    def test_too_few_records_raises_and_does_not_save(self):
        storage = Storage(exists=False)
        url = "https://example.test/snap.csv"
        data = valid_snapshot_bytes(1)
        worker = OpenSkyWorker(
            storage=storage,
            find_latest=lambda: url,
            download=lambda u: data,
            min_records=100,
            emit=lambda _: None,
        )
        with self.assertRaisesRegex(ValueError, "only 1 records"):
            worker.process_once()
        self.assertEqual(storage.saved, [])

    def test_find_latest_failure_propagates_without_checking_or_saving(self):
        storage = Storage(exists=False)

        def fail_find_latest():
            raise OSError("offline")

        worker = OpenSkyWorker(
            storage=storage,
            find_latest=fail_find_latest,
            download=MagicMock(),
            min_records=1,
            emit=lambda _: None,
        )
        with self.assertRaisesRegex(OSError, "offline"):
            worker.process_once()
        self.assertEqual((storage.checked, storage.saved), ([], []))


class RunForeverTests(unittest.TestCase):
    def test_one_worker_failing_does_not_skip_the_others_in_the_same_cycle(self):
        worker1 = MagicMock()
        worker1.process_once.side_effect = Exception("boom")
        worker2 = MagicMock()
        worker2.process_once.return_value = {"status": "ok"}
        sleep = MagicMock(side_effect=RuntimeError("stop-test-loop"))
        with self.assertRaisesRegex(RuntimeError, "stop-test-loop"):
            run_forever([worker1, worker2], sleep=sleep)
        worker1.process_once.assert_called_once()
        worker2.process_once.assert_called_once()

    def test_once_true_raises_on_failure_without_sleeping(self):
        worker1 = MagicMock()
        worker1.process_once.side_effect = Exception("boom")
        worker2 = MagicMock()
        sleep = MagicMock()
        with self.assertRaisesRegex(Exception, "boom"):
            run_forever([worker1, worker2], once=True, sleep=sleep)
        worker1.process_once.assert_called_once()
        worker2.process_once.assert_not_called()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
