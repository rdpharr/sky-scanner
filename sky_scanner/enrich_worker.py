"""Daily FAA registry downloader/importer, independent of aircraft collection."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

from . import opensky
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


def run_forever(
    worker: Any,
    *,
    once: bool = False,
    retry_seconds: float = 5,
    max_retry_seconds: float = 300,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Retry failures with bounded exponential backoff; import daily on success.

    ``worker`` may be one worker or a list; each source is attempted every
    cycle, and one source failing never skips the others.
    """
    workers = list(worker) if isinstance(worker, (list, tuple)) else [worker]
    delay = retry_seconds
    pending = list(workers)
    while True:
        failed = []
        for item in pending:
            try:
                item.process_once()
            except Exception as exc:
                print(f"{type(item).__name__} attempt failed: {exc}", file=sys.stderr, flush=True)
                if once:
                    raise
                failed.append(item)
        if once:
            return
        if failed:
            pending = failed
            sleep(delay)
            delay = min(max_retry_seconds, max(retry_seconds, delay * 2))
            continue
        delay = retry_seconds
        pending = list(workers)
        sleep(DAILY_SECONDS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import the free FAA aircraft registry")
    parser.add_argument("--once", action="store_true", help="run a single import attempt")
    args = parser.parse_args(argv)
    try:
        min_records = int(os.environ.get("SKY_SCANNER_ENRICHMENT_MIN_RECORDS", DEFAULT_MIN_RECORDS))
        workers: list[Any] = [EnrichmentWorker(min_records=min_records)]
        if os.environ.get("OPENSKY_ENABLED", "1") != "0":
            workers.append(OpenSkyWorker())
        run_forever(workers, once=args.once)
    except Exception as exc:
        print(f"FAA enrichment worker stopped: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
