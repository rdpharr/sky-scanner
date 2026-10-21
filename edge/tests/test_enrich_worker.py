import io
import json
import tempfile
import unittest
import urllib.error
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from sky_scanner import military, tar1090db
from sky_scanner.enrich_worker import (
    DAILY_SECONDS,
    DOC8643_URL,
    DOWNLOAD_HEADERS,
    DOWNLOAD_URL,
    CallwordWorker,
    EnrichmentWorker,
    MilitaryReferenceWorker,
    OpenSkyWorker,
    References,
    ReferenceWorker,
    Tar1090DbWorker,
    TypeImageWorker,
    build_workers,
    download_registry,
    run_forever,
)
from sky_scanner.heard import Heard
from sky_scanner.lookups import LookupCache

NOW = 1_800_000_000.0
DAY = 86400


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


PLANE_ALERT_CSV = (
    "ICAO,Registration,Operator,Type,ICAO Type,Category,Tag 1,Tag 2,Tag 3\n"
    "AE1001,N100AF,US Air Force,C-17,C17,Military,Cargo,,\n"
    "43C001,ZZ001,Royal Air Force,Voyager,A332,Military,Tanker,,\n"
)
OPERATOR_CODES_CSV = (
    "Code,Name,ICAO,IATA,PositioningFlightPattern,CharterFlightPattern\n"
    "1,Air Mobility Command,RCH,,,\n"
    "2,Southwest Airlines,SWA,,,\n"
)
# hex -> (registration, type, flags, description, year, owner); flags "10" = military.
TAR1090_TABLE = {
    "A00001": ("N1", "B738", "00", "BOEING 737-800", None, None),
    "A00002": ("N2", "B738", "00", "BOEING 737-800", None, None),
    "A00003": ("N3", "C172", "00", "CESSNA 172", None, None),
    "A00004": ("N4", "BE20", "10", "BEECH KING AIR 200", None, None),
    "A00005": ("miscode", "XXXX", "10", "miscode", None, None),
}


class Storage:
    """Records what workers save; ``imported`` holds (url, hash) pairs that exist."""

    def __init__(self, imported=(), seed_rows=()):
        self.imported = set(imported)
        self.seed_rows = list(seed_rows)
        self.metadata, self.codes, self.callwords, self.type_images, self.csv, self.queries = [], [], [], [], [], []

    def import_exists(self, url, dataset_hash=None):
        return any(u == url and (dataset_hash is None or h == dataset_hash) for u, h in self.imported)

    def save_metadata_import(self, records, **kwargs):
        self.metadata.append((list(records), kwargs))
        return "import-uuid"

    def replace_military_operator_codes(self, codes, **kwargs):
        self.codes.append((codes, kwargs))
        return "codes-uuid"

    def save_callword_lookup(self, word, status, url, data, fetched_at=None):
        self.callwords.append((word, status, url, data, fetched_at))

    def save_type_image(self, designator, model_name, method, result, fetched_at=None):
        self.type_images.append((designator, model_name, method, result, fetched_at))

    def load_reference_csv(self, table, data):
        self.csv.append((table, data))

    def query(self, sql, params=None):
        self.queries.append(sql)
        return self.seed_rows


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.heard_path = self.dir / "heard.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write_heard(self, aircraft, seeded=True):
        """aircraft: {address: (last_seen, {callsign: last_used})}"""
        heard = Heard(self.heard_path)
        heard.update(
            {
                "address_type": "icao",
                "address": address,
                "first_received_at": last_seen,
                "last_received_at": last_seen,
                "fields": {},
            }
            for address, (last_seen, _) in aircraft.items()
        )
        for address, (_, callsigns) in aircraft.items():
            heard.aircraft[("icao", address)].callsigns.update(callsigns)
        heard.seeded = seeded
        heard.save()

    def references(self, **overrides):
        refs = References(
            plane_alert={r["icao_address"]: r for r in military.parse_plane_alert_mil(PLANE_ALERT_CSV.encode())},
            operator_codes={"RCH": "Air Mobility Command"},
            tar1090=dict(TAR1090_TABLE),
            tar1090_digest="digest1",
        )
        for name, value in overrides.items():
            setattr(refs, name, value)
        return refs


# --- FAA and OpenSky ----------------------------------------------------------


class EnrichmentWorkerTests(unittest.TestCase):
    def worker(self, storage, **kwargs):
        kwargs.setdefault("download", lambda: registry_zip())
        return EnrichmentWorker(
            storage=storage, min_records=kwargs.pop("min_records", 2), emit=lambda _: None, **kwargs
        )

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

        def fail():
            raise OSError("offline")

        with self.assertRaisesRegex(OSError, "offline"):
            self.worker(storage, download=fail).process_once()
        self.assertEqual(storage.metadata, [])

    def test_empty_registry_rejected_without_import(self):
        storage = Storage()
        with self.assertRaisesRegex(ValueError, "only 0 records"):
            self.worker(storage, download=lambda: registry_zip(0), min_records=1).process_once()
        self.assertEqual(storage.metadata, [])

    def test_duplicate_dataset_hash_is_skipped(self):
        first = self.worker(Storage()).process_once()
        storage = Storage(imported={(DOWNLOAD_URL, first["dataset_hash"])})
        output = []
        worker = self.worker(storage)
        worker.emit = output.append
        summary = worker.process_once()
        self.assertEqual(summary["status"], "skipped")
        self.assertEqual(storage.metadata, [])
        self.assertNotIn("Owner", output[0])
        self.assertEqual(json.loads(output[0])["status"], "skipped")

    def test_new_registry_is_imported(self):
        storage = Storage()
        summary = self.worker(storage).process_once()
        self.assertEqual((summary["status"], summary["import_id"]), ("imported", "import-uuid"))
        records, kwargs = storage.metadata[0]
        self.assertEqual(len(records), 2)
        self.assertEqual(kwargs["source_url"], DOWNLOAD_URL)
        self.assertEqual(kwargs["dataset_hash"], summary["dataset_hash"])


class OpenSkyWorkerTests(unittest.TestCase):
    URL = "https://example.test/aircraft-database-complete-2026-09.csv"

    def test_already_imported_snapshot_is_not_downloaded(self):
        download = MagicMock()
        storage = Storage(imported={(self.URL, "anything")})
        worker = OpenSkyWorker(storage=storage, find_latest=lambda: self.URL, download=download, emit=lambda _: None)
        self.assertEqual(worker.process_once()["status"], "skipped")
        download.assert_not_called()

    def test_new_snapshot_is_imported_as_its_own_source(self):
        csv = "icao24,registration,model,typecode,operator,owner\nabc123,N1,737,B738,,Owner\n"
        storage = Storage()
        worker = OpenSkyWorker(
            storage=storage,
            find_latest=lambda: self.URL,
            download=lambda url: csv.encode(),
            min_records=1,
            emit=lambda _: None,
        )
        self.assertEqual(worker.process_once()["status"], "imported")
        _, kwargs = storage.metadata[0]
        self.assertEqual((kwargs["source_name"], kwargs["source_url"]), ("opensky", self.URL))


# --- Military reference data --------------------------------------------------


class MilitaryReferenceWorkerTests(unittest.TestCase):
    def worker(self, storage, references, min_aircraft=1):
        return MilitaryReferenceWorker(
            storage=storage,
            references=references,
            download_plane_alert=lambda: PLANE_ALERT_CSV.encode(),
            download_operators=lambda: OPERATOR_CODES_CSV.encode(),
            min_aircraft=min_aircraft,
            emit=lambda _: None,
        )

    def hashes(self):
        import hashlib

        return {
            (military.PLANE_ALERT_MIL_URL, hashlib.sha256(PLANE_ALERT_CSV.encode()).hexdigest()),
            (military.OPERATOR_CODES_URL, hashlib.sha256(OPERATOR_CODES_CSV.encode()).hexdigest()),
        }

    def test_unchanged_data_is_not_saved_but_still_shared(self):
        storage, references = Storage(imported=self.hashes()), References()
        summary = self.worker(storage, references).process_once()
        self.assertEqual((summary["plane_alert"], summary["operator_codes"]), ("skipped", "skipped"))
        self.assertEqual((storage.metadata, storage.codes), ([], []))
        self.assertEqual(set(references.plane_alert), {"AE1001", "43C001"})
        self.assertEqual(references.operator_codes, {"RCH": "Air Mobility Command"})

    def test_new_data_is_imported(self):
        storage = Storage()
        summary = self.worker(storage, References()).process_once()
        self.assertEqual(summary["plane_alert"], "imported 2")
        self.assertEqual(storage.metadata[0][1]["source_name"], military.PLANE_ALERT_SOURCE)
        codes, kwargs = storage.codes[0]
        self.assertEqual([c["code"] for c in codes], ["RCH"])
        self.assertEqual(kwargs["source_url"], military.OPERATOR_CODES_URL)

    def test_too_few_aircraft_raises_without_sharing(self):
        references = References()
        with self.assertRaisesRegex(ValueError, "only 2 aircraft"):
            self.worker(Storage(), references, min_aircraft=100).process_once()
        self.assertIsNone(references.plane_alert)


class ReferencesTests(TempDirTestCase):
    def test_military_check_uses_every_rule(self):
        refs = self.references()
        self.assertIn("US military ICAO address block", refs.military_evidence("AE9999", []))
        self.assertIn("plane-alert-db military list", refs.military_evidence("43C001", []))
        self.assertIn("tar1090-db military flag", refs.military_evidence("A00004", []))
        self.assertIn(
            "military operator code (Air Mobility Command)", refs.military_evidence("A00001", ["SWA1", "RCH123"])
        )
        self.assertEqual(refs.military_evidence("A00001", ["SWA1", "RCH"]), [])

    def test_miscode_rows_are_never_military(self):
        self.assertEqual(self.references().military_evidence("A00005", []), [])

    def test_not_ready_until_every_source_is_loaded(self):
        self.assertFalse(References(plane_alert={}, operator_codes={}).military_ready())
        self.assertTrue(References(plane_alert={}, operator_codes={}, tar1090={}).military_ready())


# --- tar1090-db -------------------------------------------------------------------


class Tar1090DbWorkerTests(TempDirTestCase):
    def worker(self, storage, references, clock=None, now=None):
        download = MagicMock(return_value=b"raw")
        worker = Tar1090DbWorker(
            storage=storage,
            references=references,
            heard_path=self.heard_path,
            download=download,
            clock=clock or (lambda: 0.0),
            now=now or (lambda: NOW),
            emit=lambda _: None,
        )
        return worker, download

    def test_waits_for_heard_json_without_losing_the_full_pass(self):
        storage, references = Storage(), References()
        worker, _ = self.worker(storage, references)
        with patch.object(tar1090db, "parse", return_value=("digest1", dict(TAR1090_TABLE))):
            self.assertEqual(worker.process_once()["status"], "waiting for heard.json")
            self.write_heard({"A00001": (NOW - 100 * DAY, {})})
            summary = worker.process_once()
        self.assertEqual(summary["matched"], 1)  # heard long ago: the full pass still covers it
        self.assertEqual(references.tar1090_digest, "digest1")

    def test_full_pass_then_only_newly_heard_and_never_twice(self):
        self.write_heard({"A00001": (NOW - DAY, {}), "FFFFFF": (NOW - DAY, {})})
        storage, references = Storage(), References()
        times = iter([NOW, NOW + 1800, NOW + 3600])
        worker, download = self.worker(storage, references, now=lambda: next(times))
        with patch.object(tar1090db, "parse", return_value=("digest1", dict(TAR1090_TABLE))):
            first = worker.process_once()
            self.write_heard({"A00001": (NOW + 100, {}), "A00002": (NOW + 100, {}), "FFFFFF": (NOW - DAY, {})})
            second = worker.process_once()
            third = worker.process_once()
        download.assert_called_once()
        self.assertEqual((first["checked"], first["matched"]), (2, 1))
        self.assertEqual((second["checked"], second["matched"]), (1, 1))  # A00001 already saved
        self.assertEqual(third["matched"], 0)
        self.assertEqual([[r["icao_address"] for r in recs] for recs, _ in storage.metadata], [["A00001"], ["A00002"]])
        _, kwargs = storage.metadata[0]
        self.assertEqual(
            (kwargs["source_name"], kwargs["partial"], kwargs["dataset_hash"]), ("tar1090_db", True, "digest1")
        )

    def test_daily_refresh_downloads_again_and_rechecks_everything(self):
        self.write_heard({"A00001": (NOW - DAY, {})})
        storage, references = Storage(), References()
        clock = [0.0]
        worker, download = self.worker(storage, references, clock=lambda: clock[0])
        with patch.object(tar1090db, "parse", return_value=("digest1", dict(TAR1090_TABLE))):
            worker.process_once()
        clock[0] = DAILY_SECONDS + 1
        with patch.object(tar1090db, "parse", return_value=("digest2", dict(TAR1090_TABLE))):
            summary = worker.process_once()
        self.assertEqual(download.call_count, 2)
        self.assertTrue(summary["refreshed"])
        self.assertEqual(summary["matched"], 1)
        self.assertEqual(storage.metadata[-1][1]["dataset_hash"], "digest2")


# --- Callwords --------------------------------------------------------------------


class CallwordWorkerTests(TempDirTestCase):
    def worker(self, storage, references=None, fetch=None, **kwargs):
        self.sleeps = []
        cache = kwargs.pop("cache", None) or LookupCache(self.dir / "callwords.json")
        return CallwordWorker(
            storage=storage,
            references=references or self.references(),
            heard_path=self.heard_path,
            cache=cache,
            fetch=fetch or (lambda word: (f"https://wwi.test/{word}", {"word": word})),
            sleep=self.sleeps.append,
            now=lambda: NOW,
            emit=lambda _: None,
            **kwargs,
        )

    def test_waits_until_heard_and_reference_data_are_ready(self):
        self.write_heard({"AE0001": (NOW, {"TALON95": NOW})})
        storage = Storage()
        worker = self.worker(storage, references=References())
        self.assertIn("waiting", worker.process_once()["status"])
        self.assertEqual((storage.callwords, storage.queries), ([], []))

    def test_looks_up_recent_callwords_of_military_aircraft_only(self):
        self.write_heard(
            {
                "AE0001": (NOW, {"TALON95": NOW - DAY, "OLDIE12": NOW - 20 * DAY}),  # US military block
                "A00001": (NOW, {"RCH123": NOW, "SHARK1": NOW}),  # operator code makes it military
                "A00002": (NOW, {"VIPER11": NOW}),  # civil
            }
        )
        storage = Storage()
        summary = self.worker(storage).process_once()
        self.assertEqual(summary["looked_up"], ["SHARK", "TALON"])
        self.assertEqual([c[:2] for c in storage.callwords], [("SHARK", "found"), ("TALON", "found")])
        self.assertEqual(self.sleeps, [5])

    def test_fresh_lookups_are_skipped_and_results_cached(self):
        self.write_heard({"AE0001": (NOW, {"TALON95": NOW, "BOXER1": NOW})})
        cache = LookupCache(self.dir / "callwords.json")
        cache.seeded = True
        cache.record("TALON", "found", NOW - 29 * DAY)
        cache.record("BOXER", "not_found", NOW - 8 * DAY)  # not-found results expire after 7 days
        storage = Storage()
        summary = self.worker(storage, cache=cache, fetch=lambda w: ("u", None)).process_once()
        self.assertEqual(summary["looked_up"], ["BOXER"])
        self.assertEqual(storage.callwords[0][1], "not_found")
        self.assertEqual(LookupCache.load(self.dir / "callwords.json").entries["BOXER"], ("not_found", NOW))

    def test_seeds_its_cache_from_bigquery_once(self):
        self.write_heard({"AE0001": (NOW, {"TALON95": NOW})})
        storage = Storage(seed_rows=[{"key": "TALON", "status": "found", "fetched_at": NOW - DAY}])
        worker = self.worker(storage)
        self.assertEqual(worker.process_once()["looked_up"], [])
        worker.process_once()
        self.assertEqual(len(storage.queries), 1)
        self.assertIn("callword_lookups", storage.queries[0])

    def test_max_lookups_limits_requests_and_reports_pending(self):
        self.write_heard({"AE0001": (NOW, {f"WORD{chr(65 + i)}1": NOW for i in range(4)})})
        storage = Storage()
        summary = self.worker(storage, max_lookups=2).process_once()
        self.assertEqual((len(summary["looked_up"]), summary["pending"]), (2, 2))
        self.assertEqual(self.sleeps, [5])


# --- Type photos ---------------------------------------------------------------------


class TypeImageWorkerTests(TempDirTestCase):
    def worker(self, storage, resolve=None, **kwargs):
        self.sleeps = []
        self.designator_fetches = 0

        def fetch_designators():
            self.designator_fetches += 1
            return {"B738": "Boeing 737 Next Generation"}

        cache = kwargs.pop("cache", None) or LookupCache(self.dir / "type_images.json")
        return TypeImageWorker(
            storage=storage,
            references=self.references(),
            heard_path=self.heard_path,
            cache=cache,
            fetch_designators=fetch_designators,
            resolve=resolve or (lambda tc, name, designators: ("search", {"title": tc})),
            sleep=self.sleeps.append,
            now=lambda: NOW,
            emit=lambda _: None,
            **kwargs,
        )

    def heard(self):
        self.write_heard(
            {
                "A00001": (NOW, {}),  # B738
                "A00002": (NOW, {}),  # B738
                "A00003": (NOW, {}),  # C172
                "A00004": (NOW, {}),  # BE20, military by tar1090 flag
                "A00005": (NOW, {}),  # miscode: no type
                "43C001": (NOW, {}),  # A332 from plane-alert
                "AE1001": (NOW - 40 * DAY, {}),  # heard too long ago
            }
        )

    def test_military_types_first_then_most_common_skipping_fresh_and_miscodes(self):
        self.heard()
        cache = LookupCache(self.dir / "type_images.json")
        cache.seeded = True
        cache.record("C172", "found", NOW - DAY)
        storage = Storage()
        summary = self.worker(storage, cache=cache).process_once()
        self.assertEqual(summary["resolved"], ["A332", "BE20", "B738"])
        self.assertEqual(storage.type_images[0][:2], ("A332", "Voyager"))
        self.assertEqual(storage.type_images[2][:2], ("B738", "BOEING 737-800"))
        self.assertEqual(self.sleeps, [3, 3])

    def test_max_types_limits_requests_and_reports_pending(self):
        self.heard()
        summary = self.worker(Storage(), max_types=1).process_once()
        self.assertEqual((summary["resolved"], summary["pending"]), (["A332"], 3))

    def test_http_429_stops_the_cycle_quietly(self):
        self.heard()

        def throttled(*_):
            raise urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)

        storage = Storage()
        summary = self.worker(storage, resolve=throttled).process_once()
        self.assertEqual((summary["resolved"], storage.type_images), ([], []))

    def test_other_http_errors_propagate(self):
        self.heard()

        def broken(*_):
            raise urllib.error.HTTPError("u", 500, "Server Error", {}, None)

        with self.assertRaises(urllib.error.HTTPError):
            self.worker(Storage(), resolve=broken).process_once()

    def test_designator_list_is_fetched_once_and_reused(self):
        self.heard()
        worker = self.worker(Storage(), max_types=1)
        worker.process_once()
        worker.process_once()
        self.assertEqual(self.designator_fetches, 1)

    def test_not_found_is_saved_and_cached_as_not_found(self):
        self.write_heard({"A00003": (NOW, {})})
        storage = Storage()
        self.worker(storage, resolve=lambda *_: ("search", None)).process_once()
        self.assertEqual(storage.type_images[0][3], None)
        self.assertEqual(LookupCache.load(self.dir / "type_images.json").entries["C172"], ("not_found", NOW))


class ReferenceWorkerTests(unittest.TestCase):
    def test_loads_doc8643_into_its_table(self):
        storage = Storage()
        summary = ReferenceWorker(storage=storage, fetch=lambda url: b"Designator\nB738\n").process_once()
        self.assertEqual(storage.csv, [("doc8643_types", b"Designator\nB738\n")])
        self.assertEqual(summary["bytes"], 16)
        self.assertTrue(DOC8643_URL.endswith("doc8643AircraftTypes.csv"))


class BuildWorkersTests(unittest.TestCase):
    def test_reference_loaders_come_first_and_switches_are_honored(self):
        storage = Storage()
        names = [w.name for w in build_workers({"CACHE_DIR": "/nonexistent"}, storage)]
        self.assertEqual(
            names,
            ["doc8643", "military_reference", "tar1090_db", "faa_registry", "opensky", "callwords", "type_images"],
        )
        workers = build_workers({"MILITARY_ENABLED": "0", "TAR1090_DB_ENABLED": "0", "OPENSKY_ENABLED": "0"}, storage)
        self.assertEqual([w.name for w in workers], ["doc8643", "faa_registry", "type_images"])
        # Switched-off sources count as loaded, so type photos do not wait for them.
        self.assertTrue(workers[-1].references.military_ready())


# --- run_forever ------------------------------------------------------------------------


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
            run_forever([bad, good], sleep=fake_sleep, clock=clock.now, retry_seconds=5, max_retry_seconds=300)
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

    def test_every_cycle_is_recorded_with_its_outcome(self):
        ok = MagicMock(process_once=MagicMock(return_value={"status": "skipped", "records": 2}))
        ok.name = "faa_registry"
        failing = MagicMock(process_once=MagicMock(side_effect=ValueError("bad file")))
        failing.name = "opensky"
        record = MagicMock()
        with self.assertRaises(ValueError):
            run_forever([ok, failing], once=True, record=record)
        (name, started, finished, status, summary, error), _ = record.call_args_list[0]
        self.assertEqual(
            (name, status, summary, error), ("faa_registry", "skipped", {"status": "skipped", "records": 2}, None)
        )
        self.assertIsInstance(started, datetime)
        self.assertEqual(started.tzinfo, UTC)
        self.assertLessEqual(started, finished)
        (name, _, _, status, summary, error), _ = record.call_args_list[1]
        self.assertEqual((name, status, summary, error), ("opensky", "error", None, "ValueError: bad file"))

    def test_a_failure_to_record_never_stops_the_worker(self):
        worker = MagicMock(process_once=MagicMock(return_value={}))
        record = MagicMock(side_effect=ConnectionError("offline"))
        with patch("sys.stderr", new=io.StringIO()) as stderr:
            run_forever(worker, once=True, record=record)
        worker.process_once.assert_called_once()
        self.assertIn("offline", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
