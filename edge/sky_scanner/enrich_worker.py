"""Enrichment workers: download free registries and lookups, write BigQuery ``raw``.

Each worker runs on its own schedule in one process (``run_forever``), and
every cycle is recorded in ``raw.enrichment_runs``. What was heard comes from
``heard.json`` (written by ingest); reference data one worker downloads (the
tar1090-db table, the plane-alert military list, military operator codes) is
shared with the others through ``References``. A worker whose inputs are not
there yet reports ``waiting`` and tries again next cycle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import military, opensky, tar1090db, wikipedia
from .bq import BigQuery
from .enrichment import parse_faa_registry
from .heard import Heard
from .lookups import SEED_SQL, LookupCache
from .storage import BigQueryStorage

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
# ICAO Doc 8643 aircraft type designators, loaded daily for dbt as text columns.
DOC8643_URL = "https://s3.opensky-network.org/data-samples/metadata/doc8643AircraftTypes.csv"
MAX_REFERENCE_BYTES = 20 * 1024 * 1024
DESIGNATOR = re.compile(r"[A-Z0-9]{2,4}")
OPERATOR_CALLSIGN = re.compile(r"^[A-Z]{3}[0-9]")


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


def fetch_reference(url: str, timeout: int = 60) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "SkyScanner/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_REFERENCE_BYTES + 1)
    if len(data) > MAX_REFERENCE_BYTES:
        raise ValueError(f"{url} is larger than {MAX_REFERENCE_BYTES} bytes")
    return data


@dataclass
class References:
    """Reference data shared by the workers of one process; None until loaded.

    A source that is switched off is set to an empty mapping instead, so
    workers that use it do not wait for it.
    """

    plane_alert: dict[str, dict[str, Any]] | None = None
    operator_codes: dict[str, str] | None = None
    tar1090: dict[str, tuple] | None = None
    tar1090_digest: str | None = None

    def military_ready(self) -> bool:
        return None not in (self.plane_alert, self.operator_codes, self.tar1090)

    def tar1090_record(self, address: str) -> dict[str, Any] | None:
        if not self.tar1090 or address.upper() not in self.tar1090:
            return None
        (record,) = tar1090db.records(self.tar1090, [address], dataset_hash=self.tar1090_digest or "")
        return record

    def military_evidence(self, address: str, callsigns: Iterable[str]) -> list[str]:
        """The edge's copy of dbt's military rules, over every callsign given."""
        codes = self.operator_codes or {}
        operator_name = next(
            (codes[c[:3]] for c in sorted(callsigns) if OPERATOR_CALLSIGN.match(c) and c[:3] in codes), None
        )
        plane_alert = (self.plane_alert or {}).get(address.upper())
        return military.military_evidence(address, plane_alert, operator_name, self.tar1090_record(address))


def _load_heard(path: str | os.PathLike) -> Heard | None:
    """``heard.json`` once ingest has seeded it; None until then."""
    heard = Heard.load(path)
    return heard if heard.seeded else None


def _ensure_seeded(cache: LookupCache, storage: Any, table: str) -> None:
    if not cache.seeded:
        cache.seed(storage.query(SEED_SQL[table]))


class EnrichmentWorker:
    """Daily FAA registry import, skipped when the file's hash was imported already."""

    name = "faa_registry"

    def __init__(
        self,
        *,
        storage: Any,
        download: Callable[[], bytes] = download_registry,
        min_records: int = DEFAULT_MIN_RECORDS,
        emit: Callable[[str], None] = print,
    ):
        if min_records < 1:
            raise ValueError("min_records must be positive")
        self.storage, self.download, self.min_records, self.emit = storage, download, min_records, emit

    def process_once(self) -> dict[str, Any]:
        """Run one import attempt, returning a privacy-safe count/hash summary."""
        archive = self.download()
        retrieved_at = datetime.now(UTC)
        records = parse_faa_registry(archive, source_url=DOWNLOAD_URL, retrieved_at=retrieved_at)
        if len(records) < self.min_records:
            raise ValueError(f"FAA registry has only {len(records)} records; minimum accepted is {self.min_records}")
        dataset_hash = records[0]["dataset_hash"]
        summary: dict[str, Any] = {"source": self.name, "records": len(records), "dataset_hash": dataset_hash}
        if self.storage.import_exists(DOWNLOAD_URL, dataset_hash):
            summary["status"] = "skipped"
        else:
            summary["import_id"] = self.storage.save_metadata_import(
                records, source_url=DOWNLOAD_URL, retrieved_at=retrieved_at, dataset_hash=dataset_hash
            )
            summary["status"] = "imported"
        self.emit(json.dumps(summary, separators=(",", ":")))
        return summary


class OpenSkyWorker:
    """Import the newest OpenSky crowdsourced snapshot as a separate source.

    The S3 listing is checked first; a snapshot URL that was already imported
    is skipped without downloading it. OpenSky values never replace FAA data.
    """

    name = "opensky"

    def __init__(
        self,
        *,
        storage: Any,
        find_latest: Callable[[], str] = opensky.find_latest_snapshot,
        download: Callable[[str], bytes] = opensky.download_snapshot,
        min_records: int = 100_000,
        emit: Callable[[str], None] = print,
    ):
        self.storage, self.find_latest, self.download = storage, find_latest, download
        self.min_records, self.emit = min_records, emit

    def process_once(self) -> dict[str, Any]:
        url = self.find_latest()
        if self.storage.import_exists(url):
            summary = {"source": self.name, "status": "skipped", "url": url}
        else:
            retrieved_at = datetime.now(UTC)
            records = opensky.parse_opensky_database(self.download(url), source_url=url, retrieved_at=retrieved_at)
            if len(records) < self.min_records:
                raise ValueError(
                    f"OpenSky database has only {len(records)} records; minimum accepted is {self.min_records}"
                )
            import_id = self.storage.save_metadata_import(
                records,
                source_url=url,
                retrieved_at=retrieved_at,
                dataset_hash=records[0]["dataset_hash"],
                source_name=opensky.SOURCE_NAME,
            )
            summary = {
                "source": self.name,
                "status": "imported",
                "url": url,
                "records": len(records),
                "import_id": import_id,
            }
        self.emit(json.dumps(summary, separators=(",", ":")))
        return summary


class MilitaryReferenceWorker:
    """Daily refresh of military identification reference data.

    Keeps the parsed plane-alert-db military list and the military ICAO
    operator codes in ``References`` for the other workers, and imports each
    into BigQuery when its content hash is new.
    """

    name = "military_reference"
    interval_seconds = DAILY_SECONDS

    def __init__(
        self,
        *,
        storage: Any,
        references: References,
        download_plane_alert: Callable[[], bytes] = military.download_plane_alert_mil,
        download_operators: Callable[[], bytes] = military.download_operator_codes,
        min_aircraft: int = 1000,
        emit: Callable[[str], None] = print,
    ):
        self.storage, self.references = storage, references
        self.download_plane_alert, self.download_operators = download_plane_alert, download_operators
        self.min_aircraft, self.emit = min_aircraft, emit

    def process_once(self) -> dict[str, Any]:
        summary: dict[str, Any] = {"source": self.name}
        data = self.download_plane_alert()
        digest = hashlib.sha256(data).hexdigest()
        records = military.parse_plane_alert_mil(data)
        if len(records) < self.min_aircraft:
            raise ValueError(f"plane-alert military list has only {len(records)} aircraft")
        self.references.plane_alert = {r["icao_address"]: r for r in records}
        if self.storage.import_exists(military.PLANE_ALERT_MIL_URL, digest):
            summary["plane_alert"] = "skipped"
        else:
            self.storage.save_metadata_import(
                records,
                source_url=military.PLANE_ALERT_MIL_URL,
                retrieved_at=records[0]["retrieved_at"],
                dataset_hash=digest,
                source_name=military.PLANE_ALERT_SOURCE,
            )
            summary["plane_alert"] = f"imported {len(records)}"
        data = self.download_operators()
        digest = hashlib.sha256(data).hexdigest()
        codes = military.parse_military_operators(data)
        self.references.operator_codes = {c["code"]: c["name"] for c in codes}
        if self.storage.import_exists(military.OPERATOR_CODES_URL, digest):
            summary["operator_codes"] = "skipped"
        else:
            self.storage.replace_military_operator_codes(
                codes, source_url=military.OPERATOR_CODES_URL, retrieved_at=datetime.now(UTC), dataset_hash=digest
            )
            summary["operator_codes"] = f"imported {len(codes)}"
        self.emit(json.dumps(summary, separators=(",", ":")))
        return summary


class Tar1090DbWorker:
    """Enrich heard aircraft from tar1090-db (refreshed daily).

    Every 30 minutes, aircraft heard since the previous cycle (every heard
    aircraft after a refresh) are looked up in the in-memory table and saved
    as a partial, change-only import. Addresses already saved from the
    current table are skipped, so most cycles write nothing.
    """

    name = "tar1090_db"
    interval_seconds = 30 * 60

    def __init__(
        self,
        *,
        storage: Any,
        references: References,
        heard_path: str | os.PathLike,
        download: Callable[[], bytes] = tar1090db.download,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], float] = time.time,
        emit: Callable[[str], None] = print,
    ):
        self.storage, self.references, self.heard_path = storage, references, heard_path
        self.download, self.clock, self.now, self.emit = download, clock, now, emit
        self._loaded_at = 0.0
        self._last_run: float | None = None
        self._full_pending = True
        self._saved: set[str] = set()

    def process_once(self) -> dict[str, Any]:
        refreshed = False
        if self.references.tar1090 is None or self.clock() - self._loaded_at > DAILY_SECONDS:
            self.references.tar1090_digest, self.references.tar1090 = tar1090db.parse(self.download())
            self._loaded_at = self.clock()
            self._full_pending, self._saved, refreshed = True, set(), True
        heard = _load_heard(self.heard_path)
        if heard is None:
            return {"source": self.name, "status": "waiting for heard.json", "refreshed": refreshed}
        started = self.now()
        since = 0.0 if self._full_pending or self._last_run is None else self._last_run - 300
        addresses = [address for _, address in heard.heard_since(since) if address not in self._saved]
        found = tar1090db.records(
            self.references.tar1090,
            addresses,
            dataset_hash=self.references.tar1090_digest,
            retrieved_at=datetime.fromtimestamp(started, UTC),
        )
        if found:
            self.storage.save_metadata_import(
                found,
                source_url=tar1090db.URL,
                retrieved_at=datetime.fromtimestamp(started, UTC),
                dataset_hash=self.references.tar1090_digest,
                source_name=tar1090db.SOURCE_NAME,
                partial=True,
            )
        self._saved.update(r["icao_address"] for r in found)
        self._last_run, self._full_pending = started, False
        summary = {"source": self.name, "checked": len(addresses), "matched": len(found), "refreshed": refreshed}
        if refreshed or found:
            self.emit(json.dumps(summary, separators=(",", ":")))
        return summary


class CallwordWorker:
    """Look up tactical callwords of military aircraft in War Watch Intel.

    Runs every 15 minutes, only for callsigns used in the last
    ``lookback_days`` by aircraft the local military check flags. Results
    are cached (found: 30 days, not found: 7 days), at most ``max_lookups``
    requests run per cycle, spaced ``pause_seconds`` apart.
    """

    name = "callwords"
    interval_seconds = 15 * 60

    def __init__(
        self,
        *,
        storage: Any,
        references: References,
        heard_path: str | os.PathLike,
        cache: LookupCache,
        fetch: Callable[[str], tuple] = military.fetch_wwi_family,
        lookback_days: int = 14,
        max_lookups: int = 10,
        pause_seconds: float = 5,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
        emit: Callable[[str], None] = print,
    ):
        self.storage, self.references, self.heard_path, self.cache = storage, references, heard_path, cache
        self.fetch, self.lookback_days, self.max_lookups = fetch, lookback_days, max_lookups
        self.pause_seconds, self.sleep, self.now, self.emit = pause_seconds, sleep, now, emit

    def process_once(self) -> dict[str, Any]:
        heard = _load_heard(self.heard_path)
        if heard is None or not self.references.military_ready():
            return {"source": self.name, "status": "waiting for heard.json and reference data"}
        _ensure_seeded(self.cache, self.storage, "callword_lookups")
        since = self.now() - self.lookback_days * 86400
        callsigns: set[str] = set()
        for (_, address), entry in heard.aircraft.items():
            recent = [c for c, used in entry.callsigns.items() if used >= since]
            if recent and self.references.military_evidence(address, entry.callsigns):
                callsigns.update(recent)
        codes = set(self.references.operator_codes)
        words = sorted(
            {
                w
                for w in (military.callword(c, codes) for c in callsigns)
                if w and not self.cache.is_fresh(w, self.now())
            }
        )
        done = []
        for index, word in enumerate(words[: self.max_lookups]):
            if index:
                self.sleep(self.pause_seconds)
            url, family = self.fetch(word)
            status = "found" if family else "not_found"
            fetched_at = self.now()
            self.storage.save_callword_lookup(word, status, url, family, fetched_at)
            self.cache.record(word, status, fetched_at)
            done.append(word)
        summary = {"source": self.name, "looked_up": done, "pending": max(0, len(words) - len(done))}
        if done:
            self.emit(json.dumps(summary, separators=(",", ":")))
        return summary


class TypeImageWorker:
    """Resolve one Wikipedia article/photo per aircraft type heard recently.

    The type and model name come from tar1090-db (or the plane-alert military
    list). Runs every 30 minutes; at most ``max_types`` types per cycle,
    military types first, then the most common, spaced ``pause_seconds``
    apart and cached (found: 30 days, not found: 7 days). An HTTP 429 from
    Wikipedia ends the cycle early.
    """

    name = "type_images"
    interval_seconds = 30 * 60

    def __init__(
        self,
        *,
        storage: Any,
        references: References,
        heard_path: str | os.PathLike,
        cache: LookupCache,
        fetch_designators: Callable[[], dict] = wikipedia.fetch_designator_list,
        resolve: Callable[..., tuple] = wikipedia.resolve,
        lookback_days: int = 30,
        max_types: int = 15,
        pause_seconds: float = 3,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
        emit: Callable[[str], None] = print,
    ):
        self.storage, self.references, self.heard_path, self.cache = storage, references, heard_path, cache
        self.fetch_designators, self.resolve = fetch_designators, resolve
        self.lookback_days, self.max_types = lookback_days, max_types
        self.pause_seconds, self.sleep, self.now, self.emit = pause_seconds, sleep, now, emit
        self._designators: dict[str, str] | None = None
        self._designators_at = 0.0

    def designators(self) -> dict[str, str]:
        if self._designators is None or time.monotonic() - self._designators_at > DAILY_SECONDS:
            self._designators = self.fetch_designators()
            self._designators_at = time.monotonic()
        return self._designators

    def _type(self, address: str) -> tuple[str | None, str | None]:
        """ICAO designator and model name from tar1090-db, else plane-alert."""
        record = self.references.tar1090_record(address) or {}
        if "miscode" in (record.get("tags") or ""):
            record = {}
        plane_alert = (self.references.plane_alert or {}).get(address.upper()) or {}
        designator = record.get("icao_type_code") or plane_alert.get("icao_type_code")
        return designator, record.get("model") or plane_alert.get("model")

    def process_once(self) -> dict[str, Any]:
        heard = _load_heard(self.heard_path)
        if heard is None or not self.references.military_ready():
            return {"source": self.name, "status": "waiting for heard.json and reference data"}
        _ensure_seeded(self.cache, self.storage, "aircraft_type_images")
        now = self.now()
        since = now - self.lookback_days * 86400
        counts: dict[str, int] = {}
        names: dict[str, str | None] = {}
        military_types: set[str] = set()
        for (_, address), entry in heard.aircraft.items():
            if entry.last_seen < since:
                continue
            designator, model = self._type(address)
            if not designator or not DESIGNATOR.fullmatch(designator) or self.cache.is_fresh(designator, now):
                continue
            counts[designator] = counts.get(designator, 0) + 1
            if not names.get(designator):
                names[designator] = model
            if self.references.military_evidence(address, entry.callsigns):
                military_types.add(designator)
        # Military types first (rare, and the ones worth a look), then busiest.
        pending = sorted(counts, key=lambda tc: (tc not in military_types, -counts[tc], tc))
        done = []
        for index, designator in enumerate(pending[: self.max_types]):
            if index:
                self.sleep(self.pause_seconds)
            try:
                method, result = self.resolve(designator, names[designator], self.designators())
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    break
                raise
            fetched_at = self.now()
            self.storage.save_type_image(designator, names[designator], method, result, fetched_at)
            self.cache.record(designator, "found" if result else "not_found", fetched_at)
            done.append(designator)
        summary = {"source": self.name, "resolved": done, "pending": max(0, len(pending) - len(done))}
        if done:
            self.emit(json.dumps(summary, separators=(",", ":")))
        return summary


class ReferenceWorker:
    """Daily load of ICAO Doc 8643 type designators into ``raw.doc8643_types``."""

    name = "doc8643"

    def __init__(self, *, storage: Any, fetch: Callable[[str], bytes] = fetch_reference, url: str = DOC8643_URL):
        self.storage, self.fetch, self.url = storage, fetch, url

    def process_once(self) -> dict[str, Any]:
        data = self.fetch(self.url)
        self.storage.load_reference_csv("doc8643_types", data)
        return {"source": self.name, "bytes": len(data)}


def run_forever(
    worker: Any,
    *,
    once: bool = False,
    record: Callable[..., None] | None = None,
    retry_seconds: float = 5,
    max_retry_seconds: float = 300,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Run each worker on its own schedule, retrying failures with backoff.

    ``worker`` may be one worker or a list. A worker's ``interval_seconds``
    (default daily) sets its cadence; one source failing never delays others.
    Each cycle is passed to ``record`` (worker name, start, end, status,
    summary, error); a failure to record is printed, never raised.
    """
    workers = list(worker) if isinstance(worker, (list, tuple)) else [worker]

    def run(item: Any) -> None:
        started = datetime.now(UTC)
        summary, error = None, None
        try:
            summary = item.process_once()
        except Exception as exc:
            error = "".join(traceback.format_exception_only(exc)).strip()
            raise
        finally:
            if record is not None:
                name = getattr(item, "name", None)
                name = name if isinstance(name, str) else type(item).__name__
                status = "error" if error else str((summary or {}).get("status", "ok"))
                try:
                    record(name, started, datetime.now(UTC), status, summary, error)
                except Exception as exc:
                    print(f"recording {name} run failed: {exc}", file=sys.stderr, flush=True)

    if once:
        for item in workers:
            run(item)
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
                run(item)
            except Exception as exc:
                print(f"{type(item).__name__} attempt failed: {exc}", file=sys.stderr, flush=True)
                due[id(item)] = clock() + delay[id(item)]
                delay[id(item)] = min(max_retry_seconds, max(retry_seconds, delay[id(item)] * 2))
                continue
            delay[id(item)] = retry_seconds
            due[id(item)] = clock() + interval(item)
        sleep(max(1.0, min(due.values()) - clock()))


def build_workers(environ: dict[str, str], storage: Any) -> list[Any]:
    """Workers enabled by the environment, reference loaders first."""
    enabled = {name: environ.get(f"{name}_ENABLED", "1") != "0" for name in ("OPENSKY", "MILITARY", "TAR1090_DB")}
    type_images = environ.get("TYPE_IMAGES_ENABLED", "1") != "0"
    heard_path = environ.get("HEARD_PATH", "/var/lib/sky-scanner/heard/heard.json")
    cache_dir = Path(environ.get("CACHE_DIR", "/var/lib/sky-scanner/cache"))
    references = References()
    if not enabled["MILITARY"]:
        references.plane_alert, references.operator_codes = {}, {}
    if not enabled["TAR1090_DB"]:
        references.tar1090 = {}
    workers: list[Any] = [ReferenceWorker(storage=storage)]
    if enabled["MILITARY"]:
        workers.append(MilitaryReferenceWorker(storage=storage, references=references))
    if enabled["TAR1090_DB"]:
        workers.append(Tar1090DbWorker(storage=storage, references=references, heard_path=heard_path))
    min_records = int(environ.get("SKY_SCANNER_ENRICHMENT_MIN_RECORDS", DEFAULT_MIN_RECORDS))
    workers.append(EnrichmentWorker(storage=storage, min_records=min_records))
    if enabled["OPENSKY"]:
        workers.append(OpenSkyWorker(storage=storage))
    if enabled["MILITARY"]:
        cache = LookupCache.load(cache_dir / "callwords.json")
        workers.append(CallwordWorker(storage=storage, references=references, heard_path=heard_path, cache=cache))
    if type_images:
        cache = LookupCache.load(cache_dir / "type_images.json")
        workers.append(TypeImageWorker(storage=storage, references=references, heard_path=heard_path, cache=cache))
    return workers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download registries and lookups into BigQuery raw")
    parser.add_argument("--once", action="store_true", help="run every worker once")
    args = parser.parse_args(argv)
    try:
        storage = BigQueryStorage(BigQuery.from_env(os.environ))
        run_forever(build_workers(dict(os.environ), storage), once=args.once, record=storage.record_run)
    except Exception as exc:
        print(f"enrichment worker stopped: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
