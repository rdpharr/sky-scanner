"""Daily FAA registry downloader/importer, independent of aircraft collection."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import aircraft_log, military, opensky, tar1090db, wikipedia
from .enrichment import FAA_REGISTRY_URL, parse_faa_registry
from .storage import PostgresStorage

DOWNLOAD_URL = "https://registry.faa.gov/database/ReleasableAircraft.zip"
DOWNLOAD_TIMEOUT_SECONDS = 60
MAX_ARCHIVE_BYTES = 150 * 1024 * 1024
DEFAULT_MIN_RECORDS = 10_000
DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Sky-Scanner/1.0"
    ),
    "Referer": "https://www.faa.gov/licenses_certificates/aircraft_certification/aircraft_registry/releasable_aircraft_download",
}
DAILY_SECONDS = 24 * 60 * 60


def download_registry(url: str = DOWNLOAD_URL, timeout: int = DOWNLOAD_TIMEOUT_SECONDS) -> bytes:
    """Download the FAA ZIP with a strict maximum response size."""
    request = urllib.request.Request(url, headers=DOWNLOAD_HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_ARCHIVE_BYTES + 1)
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError(f"FAA registry archive exceeds {MAX_ARCHIVE_BYTES} bytes")
    if not data:
        raise ValueError("FAA registry download was empty")
    return data


def _query_one(storage: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
    cursor = storage.connection.cursor()
    try:
        cursor.execute(sql, params)
        row = cursor.fetchone()
        return row[0] if row else None
    finally:
        cursor.close()


class EnrichmentWorker:
    """One cycle of bounded download, validation, deduplication, and import.

    ``storage_factory`` and ``download`` are injectable to support offline
    tests. Database readiness is checked with a read-only query; this worker
    never creates or alters schema. A missing schema therefore causes the
    supervisor loop to retry until the ingest service initializes it.
    """

    def __init__(
        self,
        *,
        storage_factory: Callable[[], Any] = PostgresStorage.from_env,
        download: Callable[[], bytes] = download_registry,
        min_records: int = DEFAULT_MIN_RECORDS,
        emit: Callable[[str], None] = print,
    ):
        if min_records < 1:
            raise ValueError("min_records must be positive")
        self.storage_factory = storage_factory
        self.download = download
        self.min_records = min_records
        self.emit = emit

    def process_once(self) -> dict[str, Any]:
        """Run one import attempt, returning a privacy-safe count/hash summary."""
        storage = self.storage_factory()
        try:
            # This succeeds only after the ingest side has initialized schema.
            _query_one(storage, "SELECT 1 FROM enrichment_imports LIMIT 1")
            archive = self.download()
            retrieved_at = datetime.now(timezone.utc)
            records = parse_faa_registry(
                archive, source_url=DOWNLOAD_URL, retrieved_at=retrieved_at
            )
            if len(records) < self.min_records:
                raise ValueError(
                    f"FAA registry has only {len(records)} records; "
                    f"minimum accepted is {self.min_records}"
                )
            dataset_hash = records[0]["dataset_hash"]
            existing = _query_one(
                storage,
                "SELECT EXISTS (SELECT 1 FROM enrichment_imports "
                "WHERE source_url = %s AND dataset_hash = %s)",
                (DOWNLOAD_URL, dataset_hash),
            )
            if existing:
                summary = {"status": "skipped", "records": len(records), "dataset_hash": dataset_hash}
                # Fields added to the importer later (registrant type, year of
                # manufacture) are filled from an unchanged dataset too.
                missing = _query_one(
                    storage,
                    "SELECT EXISTS (SELECT 1 FROM aircraft_metadata WHERE source_name = %s "
                    "AND registry_status = 'present' AND registrant_type IS NULL "
                    "AND year_mfr IS NULL LIMIT 1)",
                    ("faa_registry",),
                )
                if missing and hasattr(storage, "fill_added_fields"):
                    summary["filled"] = storage.fill_added_fields(records)
            else:
                import_id = storage.save_metadata_import(
                    records,
                    source_url=DOWNLOAD_URL,
                    retrieved_at=retrieved_at,
                    dataset_hash=dataset_hash,
                )
                summary = {
                    "status": "imported",
                    "records": len(records),
                    "dataset_hash": dataset_hash,
                    "import_id": import_id,
                }
            self.emit(json.dumps(summary, separators=(",", ":")))
            return summary
        finally:
            close = getattr(getattr(storage, "connection", None), "close", None)
            if callable(close):
                close()


class OpenSkyWorker:
    """Import the newest OpenSky crowdsourced snapshot as a separate source.

    The S3 listing is checked first; a snapshot URL that was already imported
    is skipped without downloading it. OpenSky values never replace FAA data.
    """

    def __init__(
        self,
        *,
        storage_factory: Callable[[], Any] = PostgresStorage.from_env,
        find_latest: Callable[[], str] = opensky.find_latest_snapshot,
        download: Callable[[str], bytes] = opensky.download_snapshot,
        min_records: int = 100_000,
        emit: Callable[[str], None] = print,
    ):
        self.storage_factory = storage_factory
        self.find_latest = find_latest
        self.download = download
        self.min_records = min_records
        self.emit = emit

    def process_once(self) -> dict[str, Any]:
        storage = self.storage_factory()
        try:
            _query_one(storage, "SELECT 1 FROM enrichment_imports LIMIT 1")
            url = self.find_latest()
            if _query_one(storage, "SELECT EXISTS (SELECT 1 FROM enrichment_imports "
                                   "WHERE source_url = %s)", (url,)):
                summary = {"source": opensky.SOURCE_NAME, "status": "skipped", "url": url}
            else:
                retrieved_at = datetime.now(timezone.utc)
                records = opensky.parse_opensky_database(
                    self.download(url), source_url=url, retrieved_at=retrieved_at)
                if len(records) < self.min_records:
                    raise ValueError(f"OpenSky database has only {len(records)} records; "
                                     f"minimum accepted is {self.min_records}")
                import_id = storage.save_metadata_import(
                    records, source_url=url, retrieved_at=retrieved_at,
                    dataset_hash=records[0]["dataset_hash"], source_name=opensky.SOURCE_NAME)
                summary = {"source": opensky.SOURCE_NAME, "status": "imported", "url": url,
                           "records": len(records), "import_id": import_id}
            self.emit(json.dumps(summary, separators=(",", ":")))
            return summary
        finally:
            close = getattr(getattr(storage, "connection", None), "close", None)
            if callable(close):
                close()


class MilitaryReferenceWorker:
    """Daily refresh of military identification reference data.

    Imports the plane-alert-db military list (hex -> operator/type) as its own
    metadata source and replaces the military ICAO operator code set. Each is
    skipped when its content hash is unchanged.
    """

    interval_seconds = DAILY_SECONDS

    def __init__(self, *, storage_factory: Callable[[], Any] = PostgresStorage.from_env,
                 download_plane_alert: Callable[[], bytes] = military.download_plane_alert_mil,
                 download_operators: Callable[[], bytes] = military.download_operator_codes,
                 min_aircraft: int = 1000, emit: Callable[[str], None] = print):
        self.storage_factory = storage_factory
        self.download_plane_alert = download_plane_alert
        self.download_operators = download_operators
        self.min_aircraft = min_aircraft
        self.emit = emit

    def process_once(self) -> dict[str, Any]:
        storage = self.storage_factory()
        try:
            _query_one(storage, "SELECT 1 FROM military_operator_codes LIMIT 1")
            summary: dict[str, Any] = {"source": "military_reference"}
            data = self.download_plane_alert()
            digest = hashlib.sha256(data).hexdigest()
            if _query_one(storage, "SELECT EXISTS (SELECT 1 FROM enrichment_imports "
                                   "WHERE source_url = %s AND dataset_hash = %s)",
                          (military.PLANE_ALERT_MIL_URL, digest)):
                summary["plane_alert"] = "skipped"
            else:
                records = military.parse_plane_alert_mil(data)
                if len(records) < self.min_aircraft:
                    raise ValueError(f"plane-alert military list has only {len(records)} aircraft")
                storage.save_metadata_import(
                    records, source_url=military.PLANE_ALERT_MIL_URL,
                    retrieved_at=records[0]["retrieved_at"], dataset_hash=digest,
                    source_name=military.PLANE_ALERT_SOURCE)
                summary["plane_alert"] = f"imported {len(records)}"
            data = self.download_operators()
            digest = hashlib.sha256(data).hexdigest()
            if _query_one(storage, "SELECT EXISTS (SELECT 1 FROM enrichment_imports "
                                   "WHERE source_url = %s AND dataset_hash = %s)",
                          (military.OPERATOR_CODES_URL, digest)):
                summary["operator_codes"] = "skipped"
            else:
                codes = military.parse_military_operators(data)
                storage.replace_military_operator_codes(
                    codes, source_url=military.OPERATOR_CODES_URL,
                    retrieved_at=datetime.now(timezone.utc), dataset_hash=digest)
                summary["operator_codes"] = f"imported {len(codes)}"
            self.emit(json.dumps(summary, separators=(",", ":")))
            return summary
        finally:
            close = getattr(getattr(storage, "connection", None), "close", None)
            if callable(close):
                close()


# Callsigns recently used by aircraft identified as military by a local check.
MILITARY_CALLSIGNS_SQL = """
SELECT DISTINCT btrim(s.callsign)
FROM aircraft_snapshots s
WHERE s.bucket_start >= now() - make_interval(days => %(days)s)
  AND btrim(coalesce(s.callsign, '')) <> ''
  AND (left(s.address, 2) IN ('AE', 'AF')
       OR EXISTS (SELECT 1 FROM aircraft_metadata m
                  WHERE m.source_name = 'plane_alert_mil' AND m.registry_status = 'present'
                    AND m.address_type = s.address_type AND m.address = s.address)
       OR left(btrim(s.callsign), 3) IN (SELECT code FROM military_operator_codes))
"""


class CallwordWorker:
    """Look up tactical callwords of military aircraft in War Watch Intel.

    Runs every 15 minutes, only for callsigns of aircraft already flagged as
    military. Results are cached (found: 30 days, not found: 7 days), at most
    ``max_lookups`` requests run per cycle, spaced ``pause_seconds`` apart.
    """

    interval_seconds = 15 * 60

    def __init__(self, *, storage_factory: Callable[[], Any] = PostgresStorage.from_env,
                 fetch: Callable[[str], tuple] = military.fetch_wwi_family,
                 lookback_days: int = 14, max_lookups: int = 10, pause_seconds: float = 5,
                 sleep: Callable[[float], None] = time.sleep, emit: Callable[[str], None] = print):
        self.storage_factory = storage_factory
        self.fetch = fetch
        self.lookback_days = lookback_days
        self.max_lookups = max_lookups
        self.pause_seconds = pause_seconds
        self.sleep = sleep
        self.emit = emit

    def process_once(self) -> dict[str, Any]:
        storage = self.storage_factory()
        try:
            cursor = storage.connection.cursor()
            try:
                cursor.execute("SELECT code FROM military_operator_codes")
                codes = {row[0] for row in cursor.fetchall()}
                cursor.execute(MILITARY_CALLSIGNS_SQL, {"days": self.lookback_days})
                callsigns = [row[0] for row in cursor.fetchall()]
                cursor.execute(
                    "SELECT word FROM callword_lookups WHERE "
                    "(status = 'found' AND fetched_at > now() - interval '30 days') OR "
                    "(status = 'not_found' AND fetched_at > now() - interval '7 days')")
                fresh = {row[0] for row in cursor.fetchall()}
            finally:
                cursor.close()
            words = sorted({w for w in (military.callword(c, codes) for c in callsigns) if w} - fresh)
            done = []
            for index, word in enumerate(words[:self.max_lookups]):
                if index:
                    self.sleep(self.pause_seconds)
                url, family = self.fetch(word)
                storage.save_callword_lookup(word, "found" if family else "not_found", url, family)
                done.append(word)
            summary = {"source": "callwords", "looked_up": done,
                       "pending": max(0, len(words) - len(done))}
            if done:
                self.emit(json.dumps(summary, separators=(",", ":")))
            return summary
        finally:
            close = getattr(getattr(storage, "connection", None), "close", None)
            if callable(close):
                close()


class TypeImageWorker:
    """Resolve one Wikipedia article/photo per aircraft type seen recently.

    Runs every 30 minutes; at most ``max_types`` types per cycle, spaced
    ``pause_seconds`` apart, cached (found: 30 days, not found: 7 days). An
    HTTP 429 from Wikipedia ends the cycle early.
    """

    interval_seconds = 30 * 60

    def __init__(self, *, storage_factory: Callable[[], Any] = PostgresStorage.from_env,
                 fetch_designators: Callable[[], dict] = wikipedia.fetch_designator_list,
                 resolve: Callable[..., tuple] = wikipedia.resolve,
                 lookback_days: int = 30, max_types: int = 15, pause_seconds: float = 3,
                 sleep: Callable[[float], None] = time.sleep, emit: Callable[[str], None] = print):
        self.storage_factory = storage_factory
        self.fetch_designators = fetch_designators
        self.resolve = resolve
        self.lookback_days = lookback_days
        self.max_types = max_types
        self.pause_seconds = pause_seconds
        self.sleep = sleep
        self.emit = emit
        self._designators: dict[str, str] | None = None
        self._designators_at = 0.0

    def designators(self) -> dict[str, str]:
        if self._designators is None or time.monotonic() - self._designators_at > DAILY_SECONDS:
            self._designators = self.fetch_designators()
            self._designators_at = time.monotonic()
        return self._designators

    def process_once(self) -> dict[str, Any]:
        storage = self.storage_factory()
        try:
            cursor = storage.connection.cursor()
            try:
                since = datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
                cursor.execute(aircraft_log.SUMMARY_SQL, {"since": since})
                rows = aircraft_log.summarize(cursor.fetchall(), {})
                cursor.execute(
                    "SELECT designator FROM aircraft_type_images WHERE "
                    "(status = 'found' AND fetched_at > now() - interval '30 days') OR "
                    "(status = 'not_found' AND fetched_at > now() - interval '7 days')")
                fresh = {row[0] for row in cursor.fetchall()}
            finally:
                cursor.close()
            counts: dict[str, int] = {}
            names: dict[str, str] = {}
            military_types: set[str] = set()
            for row in rows:
                tc = row["tc"]
                if tc and re.fullmatch(r"[A-Z0-9]{2,4}", tc) and tc not in fresh:
                    counts[tc] = counts.get(tc, 0) + 1
                    names.setdefault(tc, row["model"])
                    if row.get("kind") == "Military":
                        military_types.add(tc)
            # Military types first (rare, and the ones worth a look), then busiest.
            pending = sorted(counts, key=lambda tc: (tc not in military_types, -counts[tc], tc))
            done = []
            for index, tc in enumerate(pending[:self.max_types]):
                if index:
                    self.sleep(self.pause_seconds)
                try:
                    method, result = self.resolve(tc, names[tc], self.designators())
                except urllib.error.HTTPError as exc:
                    if exc.code == 429:
                        break
                    raise
                storage.save_type_image(tc, names[tc], method, result)
                done.append(tc)
            summary = {"source": "type_images", "resolved": done,
                       "pending": max(0, len(pending) - len(done))}
            if done:
                self.emit(json.dumps(summary, separators=(",", ":")))
            return summary
        finally:
            close = getattr(getattr(storage, "connection", None), "close", None)
            if callable(close):
                close()


class Tar1090DbWorker:
    """Enrich observed aircraft from tar1090-db (refreshed daily).

    Every 30 minutes, aircraft observed since the previous cycle (all
    observed aircraft on the first cycle) are looked up in the in-memory
    table and saved as a partial, change-only import. The archive is
    downloaded again when the in-memory copy is a day old.
    """

    interval_seconds = 30 * 60

    def __init__(self, *, storage_factory: Callable[[], Any] = PostgresStorage.from_env,
                 download: Callable[[], bytes] = tar1090db.download,
                 clock: Callable[[], float] = time.monotonic, emit: Callable[[str], None] = print):
        self.storage_factory = storage_factory
        self.download = download
        self.clock = clock
        self.emit = emit
        self._table: dict[str, tuple] | None = None
        self._digest: str | None = None
        self._loaded_at = 0.0
        self._last_run: datetime | None = None

    def process_once(self) -> dict[str, Any]:
        storage = self.storage_factory()
        try:
            refreshed = False
            if self._table is None or self.clock() - self._loaded_at > DAILY_SECONDS:
                self._digest, self._table = tar1090db.parse(self.download())
                self._loaded_at = self.clock()
                refreshed = True
            started = datetime.now(timezone.utc)
            cursor = storage.connection.cursor()
            try:
                if refreshed or self._last_run is None:
                    cursor.execute("SELECT DISTINCT address FROM aircraft_snapshots")
                else:
                    cursor.execute("SELECT DISTINCT address FROM aircraft_snapshots "
                                   "WHERE bucket_start >= %s", (self._last_run - timedelta(minutes=5),))
                addresses = [row[0] for row in cursor.fetchall()]
            finally:
                cursor.close()
            found = tar1090db.records(self._table, addresses, dataset_hash=self._digest,
                                      retrieved_at=started)
            if found:
                storage.save_metadata_import(
                    found, source_url=tar1090db.URL, retrieved_at=started,
                    dataset_hash=self._digest, source_name=tar1090db.SOURCE_NAME, partial=True)
            self._last_run = started
            summary = {"source": tar1090db.SOURCE_NAME, "checked": len(addresses),
                       "matched": len(found), "refreshed": refreshed}
            if refreshed or found:
                self.emit(json.dumps(summary, separators=(",", ":")))
            return summary
        finally:
            close = getattr(getattr(storage, "connection", None), "close", None)
            if callable(close):
                close()


def run_forever(
    worker: Any,
    *,
    once: bool = False,
    retry_seconds: float = 5,
    max_retry_seconds: float = 300,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Run each worker on its own schedule, retrying failures with backoff.

    ``worker`` may be one worker or a list. A worker's ``interval_seconds``
    (default daily) sets its cadence; one source failing never delays others.
    """
    workers = list(worker) if isinstance(worker, (list, tuple)) else [worker]
    if once:
        for item in workers:
            item.process_once()
        return

    def interval(item: Any) -> float:
        value = getattr(item, "interval_seconds", DAILY_SECONDS)
        return float(value) if isinstance(value, (int, float)) else DAILY_SECONDS

    due = {id(item): clock() for item in workers}
    delay = {id(item): retry_seconds for item in workers}
    while True:
        for item in workers:
            if clock() < due[id(item)]:
                continue
            try:
                item.process_once()
            except Exception as exc:
                print(f"{type(item).__name__} attempt failed: {exc}", file=sys.stderr, flush=True)
                due[id(item)] = clock() + delay[id(item)]
                delay[id(item)] = min(max_retry_seconds, max(retry_seconds, delay[id(item)] * 2))
                continue
            delay[id(item)] = retry_seconds
            due[id(item)] = clock() + interval(item)
        sleep(max(1.0, min(due.values()) - clock()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import the free FAA aircraft registry")
    parser.add_argument("--once", action="store_true", help="run a single import attempt")
    args = parser.parse_args(argv)
    try:
        min_records = int(os.environ.get("SKY_SCANNER_ENRICHMENT_MIN_RECORDS", DEFAULT_MIN_RECORDS))
        workers: list[Any] = [EnrichmentWorker(min_records=min_records)]
        if os.environ.get("OPENSKY_ENABLED", "1") != "0":
            workers.append(OpenSkyWorker())
        if os.environ.get("MILITARY_ENABLED", "1") != "0":
            workers += [MilitaryReferenceWorker(), CallwordWorker()]
        if os.environ.get("TAR1090_DB_ENABLED", "1") != "0":
            workers.append(Tar1090DbWorker())
        if os.environ.get("TYPE_IMAGES_ENABLED", "1") != "0":
            workers.append(TypeImageWorker())
        run_forever(workers, once=args.once)
    except Exception as exc:
        print(f"FAA enrichment worker stopped: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
