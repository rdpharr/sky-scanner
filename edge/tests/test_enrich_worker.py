import hashlib
import io
import json
import unittest
import urllib.error
import zipfile
from unittest.mock import MagicMock, call, patch

from sky_scanner import military
from sky_scanner.enrich_worker import (
    DAILY_SECONDS,
    DOWNLOAD_HEADERS,
    DOWNLOAD_URL,
    CallwordWorker,
    EnrichmentWorker,
    MilitaryReferenceWorker,
    Tar1090DbWorker,
    TypeImageWorker,
    download_registry,
    run_forever,
)


def registry_zip(n=2):
    master = ["N-NUMBER,Mfr Mdl Code,Registrant Name,Mode S Code Hex"]
    for i in range(n):
        master.append(f"{1000 + i},0000001,Owner {i},{i + 1:06X}")
    ref = "Code,Manufacturer,Model,Type Aircraft\n0000001,Maker,Model,4\n"
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("MASTER.txt", "\n".join(master) + "\n")
        zf.writestr("ACFTREF.txt", ref)
    return archive.getvalue()


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = None

    def execute(self, sql, params=()):
        self.connection.queries.append((sql, params))
        if "SELECT EXISTS" in sql:
            self.result = (self.connection.exists,)
        else:
            self.result = (1,)

    def fetchone(self):
        return self.result

    def close(self):
        pass


class Connection:
    def __init__(self, exists=False):
        self.exists = exists
        self.queries = []
        self.closed = False

    def cursor(self):
        return Cursor(self)

    def close(self):
        self.closed = True


class Storage:
    def __init__(self, exists=False):
        self.connection = Connection(exists)
        self.saved = []

    def save_metadata_import(self, records, **kwargs):
        self.saved.append((records, kwargs))
        return "import-uuid"


class EnrichmentWorkerTests(unittest.TestCase):
    @patch("sky_scanner.enrich_worker.urllib.request.urlopen")
    def test_download_uses_faa_page_referrer_and_bounds_read(self, urlopen):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def read(self, limit):
                self.limit = limit
                return b"zip"

        response = Response()
        urlopen.return_value = response
        self.assertEqual(download_registry(), b"zip")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, DOWNLOAD_URL)
        self.assertEqual(request.get_header("Referer"), DOWNLOAD_HEADERS["Referer"])
        self.assertIn("Sky-Scanner/1.0", request.get_header("User-agent"))
        self.assertEqual(response.limit, 150 * 1024 * 1024 + 1)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 60)

    def test_download_failure_never_attempts_import(self):
        storage = Storage()

        def fail_download():
            raise OSError("offline")

        worker = EnrichmentWorker(storage_factory=lambda: storage, download=fail_download, min_records=1, emit=lambda _: None)
        with self.assertRaisesRegex(OSError, "offline"):
            worker.process_once()
        self.assertEqual(storage.saved, [])
        self.assertTrue(storage.connection.closed)

    def test_empty_registry_rejected_without_import(self):
        storage = Storage()
        worker = EnrichmentWorker(
            storage_factory=lambda: storage, download=lambda: registry_zip(0), min_records=1,
            emit=lambda _: None,
        )
        with self.assertRaisesRegex(ValueError, "only 0 records"):
            worker.process_once()
        self.assertEqual(storage.saved, [])

    def test_duplicate_dataset_hash_is_skipped(self):
        storage = Storage(exists=True)
        output = []
        worker = EnrichmentWorker(
            storage_factory=lambda: storage, download=lambda: registry_zip(), min_records=2,
            emit=output.append,
        )
        summary = worker.process_once()
        self.assertEqual(summary["status"], "skipped")
        self.assertEqual(summary["records"], 2)
        self.assertEqual(len(summary["dataset_hash"]), 64)
        self.assertEqual(storage.saved, [])
        self.assertNotIn("Owner", output[0])
        self.assertEqual(json.loads(output[0])["status"], "skipped")

    def test_unchanged_dataset_fills_fields_added_later(self):
        class FillingStorage(Storage):
            def fill_added_fields(self, records):
                self.filled_from = records
                return len(records)

        storage = FillingStorage(exists=True)  # dataset imported, fields missing
        worker = EnrichmentWorker(
            storage_factory=lambda: storage, download=lambda: registry_zip(), min_records=2,
            emit=lambda _: None,
        )
        summary = worker.process_once()
        self.assertEqual((summary["status"], summary["filled"]), ("skipped", 2))
        self.assertEqual(storage.saved, [])
        self.assertTrue(any("registrant_type IS NULL" in sql for sql, _ in storage.connection.queries))

    def test_valid_registry_saved_atomically_via_storage_adapter(self):
        storage = Storage()
        worker = EnrichmentWorker(
            storage_factory=lambda: storage, download=lambda: registry_zip(), min_records=2,
            emit=lambda _: None,
        )
        summary = worker.process_once()
        self.assertEqual(summary["status"], "imported")
        self.assertEqual(summary["import_id"], "import-uuid")
        records, kwargs = storage.saved[0]
        self.assertEqual(len(records), 2)
        self.assertEqual(kwargs["source_url"], DOWNLOAD_URL)
        self.assertEqual(kwargs["dataset_hash"], summary["dataset_hash"])


# --- MilitaryReferenceWorker --------------------------------------------------

PLANE_ALERT_CSV = (
    "ICAO,Registration,Operator,Type,ICAO Type,Category,Tag 1,Tag 2,Tag 3\n"
    "AE1001,N100AF,US Air Force,C-17,C17,Military,Cargo,,\n"
    "AE1002,N100NV,US Navy,P-8,P8,Military,Patrol,,\n"
)
OPERATOR_CODES_CSV = (
    "Code,Name,ICAO,IATA,PositioningFlightPattern,CharterFlightPattern\n"
    "1,Air Mobility Command,RCH,,,\n"
    "2,Southwest Airlines,SWA,,,\n"
)


class MilCursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = None

    def execute(self, sql, params=()):
        self.connection.queries.append((sql, params))
        if "SELECT EXISTS" in sql:
            url = params[0] if params else None
            self.result = (url in self.connection.exists_urls,)
        else:
            self.result = (1,)

    def fetchone(self):
        return self.result

    def close(self):
        pass


class MilConnection:
    def __init__(self, exists_urls=()):
        self.exists_urls = set(exists_urls)
        self.queries = []
        self.closed = False

    def cursor(self):
        return MilCursor(self)

    def close(self):
        self.closed = True


class MilStorage:
    def __init__(self, exists_urls=()):
        self.connection = MilConnection(exists_urls)
        self.metadata_saves = []
        self.operator_saves = []

    def save_metadata_import(self, records, **kwargs):
        self.metadata_saves.append((records, kwargs))
        return "import-uuid"

    def replace_military_operator_codes(self, codes, **kwargs):
        self.operator_saves.append((codes, kwargs))
        return "ops-uuid"


class MilitaryReferenceWorkerTests(unittest.TestCase):
    def make_worker(self, storage, min_aircraft=1):
        return MilitaryReferenceWorker(
            storage_factory=lambda: storage,
            download_plane_alert=lambda: PLANE_ALERT_CSV.encode(),
            download_operators=lambda: OPERATOR_CODES_CSV.encode(),
            min_aircraft=min_aircraft,
            emit=lambda _: None,
        )

    def test_unchanged_hashes_are_skipped_without_saving(self):
        storage = MilStorage(exists_urls={military.PLANE_ALERT_MIL_URL, military.OPERATOR_CODES_URL})
        worker = self.make_worker(storage)
        summary = worker.process_once()
        self.assertEqual(summary["plane_alert"], "skipped")
        self.assertEqual(summary["operator_codes"], "skipped")
        self.assertEqual(storage.metadata_saves, [])
        self.assertEqual(storage.operator_saves, [])
        self.assertTrue(storage.connection.closed)

    def test_new_plane_alert_data_is_imported(self):
        storage = MilStorage(exists_urls={military.OPERATOR_CODES_URL})
        worker = self.make_worker(storage, min_aircraft=2)
        summary = worker.process_once()
        self.assertIn("imported 2", summary["plane_alert"])
        self.assertEqual(len(storage.metadata_saves), 1)
        records, kwargs = storage.metadata_saves[0]
        self.assertEqual(kwargs["source_name"], military.PLANE_ALERT_SOURCE)
        self.assertEqual(len(records), 2)
        self.assertTrue(storage.connection.closed)

    def test_too_few_aircraft_raises_and_closes_connection(self):
        storage = MilStorage()
        worker = self.make_worker(storage, min_aircraft=5)
        with self.assertRaisesRegex(ValueError, "only 2"):
            worker.process_once()
        self.assertEqual(storage.metadata_saves, [])
        self.assertTrue(storage.connection.closed)

    def test_new_operator_data_replaces_codes(self):
        storage = MilStorage(exists_urls={military.PLANE_ALERT_MIL_URL})
        worker = self.make_worker(storage, min_aircraft=1)
        summary = worker.process_once()
        self.assertIn("imported", summary["operator_codes"])
        self.assertEqual(len(storage.operator_saves), 1)
        codes, kwargs = storage.operator_saves[0]
        self.assertEqual({c["code"] for c in codes}, {"RCH"})
        self.assertTrue(storage.connection.closed)


# --- CallwordWorker ------------------------------------------------------------

class CallwordStorage:
    def __init__(self, codes_rows, callsigns_rows, fresh_rows):
        cursor = MagicMock()
        cursor.fetchall.side_effect = [codes_rows, callsigns_rows, fresh_rows]
        connection = MagicMock()
        connection.cursor.return_value = cursor
        self.connection = connection
        self.saved = []

    def save_callword_lookup(self, word, status, url, data):
        self.saved.append((word, status, url, data))


class CallwordWorkerTests(unittest.TestCase):
    def test_fetches_only_new_military_callsigns(self):
        storage = CallwordStorage(
            codes_rows=[("RCH",)],
            callsigns_rows=[("TALON95",), ("RCH123",), ("STLJW21",), ("N18NY",)],
            fresh_rows=[("STLJW",)],
        )
        fetch = MagicMock(return_value=("https://wwi/TALON", {"types": []}))
        sleep = MagicMock()
        worker = CallwordWorker(storage_factory=lambda: storage, fetch=fetch, sleep=sleep,
                                emit=lambda _: None)
        summary = worker.process_once()
        fetch.assert_called_once_with("TALON")
        self.assertEqual(summary["looked_up"], ["TALON"])
        self.assertEqual(summary["pending"], 0)
        self.assertEqual(storage.saved, [("TALON", "found", "https://wwi/TALON", {"types": []})])
        self.assertTrue(storage.connection.close.called)

    def test_not_found_result_is_saved_as_not_found(self):
        storage = CallwordStorage(
            codes_rows=[], callsigns_rows=[("GHOST1",)], fresh_rows=[],
        )
        fetch = MagicMock(return_value=("https://wwi/GHOST", None))
        worker = CallwordWorker(storage_factory=lambda: storage, fetch=fetch, sleep=MagicMock(),
                                emit=lambda _: None)
        worker.process_once()
        self.assertEqual(storage.saved, [("GHOST", "not_found", "https://wwi/GHOST", None)])

    def test_max_lookups_limits_requests_and_sleeps_between_not_before_first(self):
        storage = CallwordStorage(
            codes_rows=[], callsigns_rows=[("ALPHA1",), ("BRAVO22",)], fresh_rows=[],
        )
        fetch = MagicMock(side_effect=[("u1", {"types": []}), ("u2", None)])
        sleep = MagicMock()
        worker = CallwordWorker(storage_factory=lambda: storage, fetch=fetch, sleep=sleep,
                                max_lookups=2, pause_seconds=3, emit=lambda _: None)
        summary = worker.process_once()
        self.assertEqual(fetch.call_args_list, [call("ALPHA"), call("BRAVO")])
        sleep.assert_called_once_with(3)
        self.assertEqual(summary["pending"], 0)

    def test_max_lookups_reports_pending_and_does_not_sleep_for_single_lookup(self):
        storage = CallwordStorage(
            codes_rows=[], callsigns_rows=[("ALPHA1",), ("BRAVO22",)], fresh_rows=[],
        )
        fetch = MagicMock(return_value=("u", {"types": []}))
        sleep = MagicMock()
        worker = CallwordWorker(storage_factory=lambda: storage, fetch=fetch, sleep=sleep,
                                max_lookups=1, emit=lambda _: None)
        summary = worker.process_once()
        self.assertEqual(summary["looked_up"], ["ALPHA"])
        self.assertEqual(summary["pending"], 1)
        sleep.assert_not_called()


# --- TypeImageWorker ------------------------------------------------------------

class TypeImageStorage:
    def __init__(self, cycles):
        """``cycles``: list of (summary_raw_rows, fresh_designator_rows) per process_once call."""
        cursor = MagicMock()
        side_effects = []
        for summary_rows, fresh_rows in cycles:
            side_effects += [summary_rows, fresh_rows]
        cursor.fetchall.side_effect = side_effects
        connection = MagicMock()
        connection.cursor.return_value = cursor
        self.connection = connection
        self.saved = []

    def save_type_image(self, tc, model, method, result):
        self.saved.append((tc, model, method, result))


class TypeImageWorkerTests(unittest.TestCase):
    def test_pending_ordered_by_frequency_skips_fresh_and_invalid(self):
        rows = (
            [{"tc": "B738", "model": "Boeing 737-800"}] * 3
            + [{"tc": "A320", "model": "Airbus A320"}] * 2
            + [{"tc": "FRESH", "model": "Fresh Type"}]
            + [{"tc": "", "model": "no designator"}]
            + [{"tc": "TOOLONG", "model": "invalid designator"}]
        )
        storage = TypeImageStorage([([], [("FRESH",)])])
        resolve = MagicMock(return_value=("search", {"title": "x"}))
        sleep = MagicMock()
        worker = TypeImageWorker(storage_factory=lambda: storage, fetch_designators=lambda: {},
                                 resolve=resolve, sleep=sleep, emit=lambda _: None)
        with patch("sky_scanner.enrich_worker.aircraft_log.summarize", return_value=rows):
            summary = worker.process_once()
        self.assertEqual(resolve.call_args_list, [
            call("B738", "Boeing 737-800", {}), call("A320", "Airbus A320", {}),
        ])
        self.assertEqual(storage.saved, [
            ("B738", "Boeing 737-800", "search", {"title": "x"}),
            ("A320", "Airbus A320", "search", {"title": "x"}),
        ])
        self.assertEqual(summary["resolved"], ["B738", "A320"])
        self.assertEqual(summary["pending"], 0)
        sleep.assert_called_once_with(3)

    def test_max_types_limits_requests_and_reports_pending(self):
        rows = [{"tc": "AAAA", "model": "m1"}, {"tc": "BBBB", "model": "m2"},
                {"tc": "CCCC", "model": "m3"}]
        storage = TypeImageStorage([([], [])])
        resolve = MagicMock(return_value=("search", None))
        sleep = MagicMock()
        worker = TypeImageWorker(storage_factory=lambda: storage, fetch_designators=lambda: {},
                                 resolve=resolve, sleep=sleep, max_types=2, pause_seconds=5,
                                 emit=lambda _: None)
        with patch("sky_scanner.enrich_worker.aircraft_log.summarize", return_value=rows):
            summary = worker.process_once()
        self.assertEqual(resolve.call_count, 2)
        sleep.assert_called_once_with(5)
        self.assertEqual(summary["resolved"], ["AAAA", "BBBB"])
        self.assertEqual(summary["pending"], 1)

    def test_http_429_stops_cycle_without_raising(self):
        rows = [{"tc": "AAAA", "model": "m1"}, {"tc": "BBBB", "model": "m2"}]
        storage = TypeImageStorage([([], [])])
        resolve = MagicMock(side_effect=urllib.error.HTTPError("http://x", 429, "too many", None, None))
        worker = TypeImageWorker(storage_factory=lambda: storage, fetch_designators=lambda: {},
                                 resolve=resolve, sleep=MagicMock(), emit=lambda _: None)
        with patch("sky_scanner.enrich_worker.aircraft_log.summarize", return_value=rows):
            summary = worker.process_once()
        self.assertEqual(summary["resolved"], [])
        self.assertEqual(summary["pending"], 2)
        self.assertEqual(storage.saved, [])

    def test_other_http_error_propagates(self):
        rows = [{"tc": "AAAA", "model": "m1"}]
        storage = TypeImageStorage([([], [])])
        resolve = MagicMock(side_effect=urllib.error.HTTPError("http://x", 500, "boom", None, None))
        worker = TypeImageWorker(storage_factory=lambda: storage, fetch_designators=lambda: {},
                                 resolve=resolve, sleep=MagicMock(), emit=lambda _: None)
        with patch("sky_scanner.enrich_worker.aircraft_log.summarize", return_value=rows):
            with self.assertRaises(urllib.error.HTTPError):
                worker.process_once()

    def test_designator_list_fetched_once_and_reused_across_cycles(self):
        storage = TypeImageStorage([([], []), ([], [])])
        fetch_designators = MagicMock(return_value={"B738": "Boeing 737"})
        resolve = MagicMock(return_value=("designator_list", {"title": "Boeing 737"}))
        worker = TypeImageWorker(storage_factory=lambda: storage, fetch_designators=fetch_designators,
                                 resolve=resolve, sleep=MagicMock(), emit=lambda _: None)
        rows = [{"tc": "B738", "model": "Boeing 737-800"}]
        with patch("sky_scanner.enrich_worker.aircraft_log.summarize", return_value=rows):
            worker.process_once()
            worker.process_once()
        fetch_designators.assert_called_once()
        self.assertEqual(resolve.call_count, 2)

    def test_connection_is_closed(self):
        storage = TypeImageStorage([([], [])])
        worker = TypeImageWorker(storage_factory=lambda: storage, fetch_designators=lambda: {},
                                 resolve=MagicMock(), sleep=MagicMock(), emit=lambda _: None)
        with patch("sky_scanner.enrich_worker.aircraft_log.summarize", return_value=[]):
            worker.process_once()
        self.assertTrue(storage.connection.close.called)


# --- run_forever ----------------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class RunForeverTests(unittest.TestCase):
    def test_worker_is_rerun_after_its_interval(self):
        clock = FakeClock()
        worker = MagicMock(interval_seconds=100)
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            clock.advance(seconds)
            if len(sleeps) >= 3:
                raise StopIteration()

        with self.assertRaises(StopIteration):
            run_forever(worker, sleep=fake_sleep, clock=clock.now)
        self.assertEqual(worker.process_once.call_count, 3)
        self.assertEqual(sleeps, [100, 100, 100])

    def test_failing_worker_backs_off_without_blocking_another(self):
        clock = FakeClock()
        good = MagicMock(interval_seconds=50)
        bad = MagicMock(interval_seconds=50)
        bad.process_once.side_effect = RuntimeError("boom")
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            clock.advance(seconds)
            if len(sleeps) >= 3:
                raise StopIteration()

        with self.assertRaises(StopIteration):
            run_forever([bad, good], sleep=fake_sleep, clock=clock.now,
                       retry_seconds=5, max_retry_seconds=300)
        # bad retries with a growing backoff (5, 10, 20...) so it is invoked
        # several times in the window where good (interval 50) only runs once.
        self.assertEqual(bad.process_once.call_count, 3)
        self.assertEqual(good.process_once.call_count, 1)

    def test_once_runs_each_worker_once_and_propagates_errors(self):
        ok = MagicMock()
        failing = MagicMock()
        failing.process_once.side_effect = RuntimeError("boom")
        with self.assertRaisesRegex(RuntimeError, "boom"):
            run_forever([ok, failing], once=True)
        ok.process_once.assert_called_once()
        failing.process_once.assert_called_once()

    def test_magicmock_worker_without_numeric_interval_defaults_to_daily(self):
        clock = FakeClock()
        worker = MagicMock()  # interval_seconds is itself a MagicMock, not numeric
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            clock.advance(seconds)
            raise StopIteration()

        with self.assertRaises(StopIteration):
            run_forever(worker, sleep=fake_sleep, clock=clock.now)
        worker.process_once.assert_called_once()
        self.assertEqual(sleeps, [DAILY_SECONDS])


# --- Tar1090DbWorker -----------------------------------------------------------

class Tar1090Cursor:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=()):
        self.connection.queries.append((sql, params))

    def fetchall(self):
        return [(a,) for a in self.connection.addresses]

    def close(self):
        pass


class Tar1090Connection:
    def __init__(self, addresses):
        self.addresses = addresses
        self.queries = []
        self.closed = False

    def cursor(self):
        return Tar1090Cursor(self)

    def close(self):
        self.closed = True


class Tar1090Storage:
    def __init__(self, addresses):
        self.connection = Tar1090Connection(addresses)
        self.saved = []

    def save_metadata_import(self, records, **kwargs):
        self.saved.append((records, kwargs))
        return "import-uuid"


TAR1090_TABLE = {"AAAAAA": ("N1", "B738", "10", "Desc", None, None)}


class Tar1090DbWorkerTests(unittest.TestCase):
    def test_first_cycle_queries_all_addresses_and_saves_partial(self):
        storage = Tar1090Storage(["AAAAAA", "BBBBBB"])
        clock = FakeClock()
        download = MagicMock(return_value=b"raw")
        with patch("sky_scanner.enrich_worker.tar1090db.parse",
                   return_value=("hash1", TAR1090_TABLE)) as parse:
            worker = Tar1090DbWorker(storage_factory=lambda: storage, download=download,
                                     clock=clock.now, emit=lambda _: None)
            summary = worker.process_once()
        parse.assert_called_once()
        download.assert_called_once()
        sql, params = storage.connection.queries[0]
        self.assertIn("SELECT DISTINCT address FROM aircraft_snapshots", sql)
        self.assertNotIn("WHERE", sql)
        self.assertEqual(params, ())
        self.assertEqual(len(storage.saved), 1)
        records, kwargs = storage.saved[0]
        self.assertEqual(len(records), 1)
        self.assertEqual(kwargs["source_name"], "tar1090_db")
        self.assertTrue(kwargs["partial"])
        self.assertEqual(summary["matched"], 1)
        self.assertTrue(storage.connection.closed)

    def test_second_cycle_within_a_day_does_not_redownload_and_queries_since_last_run(self):
        storage = Tar1090Storage(["AAAAAA"])
        clock = FakeClock()
        download = MagicMock(return_value=b"raw")
        with patch("sky_scanner.enrich_worker.tar1090db.parse",
                   return_value=("hash1", TAR1090_TABLE)) as parse:
            worker = Tar1090DbWorker(storage_factory=lambda: storage, download=download,
                                     clock=clock.now, emit=lambda _: None)
            worker.process_once()
            clock.advance(60)
            storage.connection.queries.clear()
            worker.process_once()
        self.assertEqual(download.call_count, 1)
        self.assertEqual(parse.call_count, 1)
        sql, params = storage.connection.queries[0]
        self.assertIn("bucket_start >=", sql)
        self.assertEqual(len(params), 1)

    def test_redownloads_after_a_day_and_queries_all_addresses_again(self):
        storage = Tar1090Storage(["AAAAAA"])
        clock = FakeClock()
        download = MagicMock(return_value=b"raw")
        with patch("sky_scanner.enrich_worker.tar1090db.parse",
                   return_value=("hash1", TAR1090_TABLE)) as parse:
            worker = Tar1090DbWorker(storage_factory=lambda: storage, download=download,
                                     clock=clock.now, emit=lambda _: None)
            worker.process_once()
            clock.advance(DAILY_SECONDS + 1)
            storage.connection.queries.clear()
            worker.process_once()
        self.assertEqual(download.call_count, 2)
        self.assertEqual(parse.call_count, 2)
        sql, params = storage.connection.queries[0]
        self.assertNotIn("WHERE", sql)
        self.assertEqual(params, ())

    def test_no_save_when_nothing_matched(self):
        storage = Tar1090Storage(["ZZZZZZ"])  # not present in the table
        clock = FakeClock()
        with patch("sky_scanner.enrich_worker.tar1090db.parse",
                   return_value=("hash1", TAR1090_TABLE)):
            worker = Tar1090DbWorker(storage_factory=lambda: storage,
                                     download=MagicMock(return_value=b"raw"),
                                     clock=clock.now, emit=lambda _: None)
            summary = worker.process_once()
        self.assertEqual(storage.saved, [])
        self.assertEqual(summary["matched"], 0)
        self.assertTrue(storage.connection.closed)


if __name__ == "__main__":
    unittest.main()
