"""Private Compose-network ingestion endpoint: snapshots to BigQuery via a disk spool.

The receiver POSTs observation and capture-interval events. Observations are
aggregated into 15-second snapshots (``snapshots.py``). Every second the
writer thread converts finished snapshots and intervals into BigQuery rows,
stamps ``written_at`` and puts them in the on-disk spool; snapshots also
update ``heard.json`` for enrichment. A sender thread streams the spool to
BigQuery ``raw`` oldest first, backing off while BigQuery or the network is
unavailable, so an outage only delays rows.
"""

from __future__ import annotations

import json
import logging
import math
import os
import signal
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from .bq import SPECS, BigQuery
from .heard import SEED_SQL, Heard
from .snapshots import SnapshotAccumulator
from .spool import DEFAULT_MAX_BYTES, Spool

LOG = logging.getLogger(__name__)

SNAPSHOTS, INTERVALS = SPECS["aircraft_snapshots"], SPECS["capture_intervals"]
# Typed snapshot columns filled from the decoded field of the same name.
_TYPED_FIELDS = {
    c.name: c.bq_type
    for c in SNAPSHOTS.columns
    if c.name in ("callsign", "squawk", "on_ground") or c.bq_type == "FLOAT64"
}


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(float(seconds), UTC).isoformat()


def _typed(value: Any, bq_type: str) -> Any:
    """Coerce a decoded value to its column type; anything unusable becomes null.

    A wrong-typed value would make BigQuery reject the whole batch.
    """
    if value is None:
        return None
    if bq_type == "STRING":
        return str(value)
    if bq_type == "BOOL":
        return value if isinstance(value, bool) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def snapshot_row(row: dict[str, Any], written_at: str) -> dict[str, Any]:
    """An accumulator snapshot as a ``raw.aircraft_snapshots`` row."""
    fields = row.get("fields") or {}
    result = {
        "bucket_start": _iso(row["bucket_start"]),
        "receiver_id": row["receiver_id"],
        "address_type": row["address_type"],
        "address": row["address"],
        "frequency_hz": int(row["frequency_hz"]),
        "first_received_at": _iso(row["first_received_at"]),
        "last_received_at": _iso(row["last_received_at"]),
        "message_count": int(row["message_count"]),
    }
    for name, bq_type in _TYPED_FIELDS.items():
        item = fields.get(name)
        result[name] = _typed(item.get("value") if isinstance(item, dict) else item, bq_type)
    result["fields"] = fields
    result["written_at"] = written_at
    return result


def interval_row(interval: dict[str, Any], written_at: str) -> dict[str, Any]:
    """A receiver capture interval as a ``raw.capture_intervals`` row."""
    ended = interval.get("ended_at")
    return {
        "id": str(interval.get("id") or uuid.uuid4()),
        "receiver_id": interval["receiver_id"],
        "frequency_hz": int(interval["frequency_hz"]),
        "started_at": _iso(interval["started_at"]),
        "ended_at": _iso(ended) if ended is not None else None,
        "decoder_version": interval.get("decoder_version"),
        "sample_config": interval.get("sample_config") or {},
        "written_at": written_at,
    }


class Ingestor:
    def __init__(self, spool: Spool, heard: Heard | None = None, *, heard_save_seconds: float = 60):
        self.accumulator = SnapshotAccumulator()
        self.lock = threading.Lock()
        self.seen = OrderedDict()
        self.snapshots = deque()
        self.snapshot_limit = 20000
        self.intervals = deque()
        self.interval_limit = 10000
        self.spool = spool
        self.heard = heard
        self.heard_save_seconds = heard_save_seconds
        self._heard_saved_at = time.monotonic()
        # Seconds after a bucket ends before it is written; later data is dropped.
        self.late_grace = float(os.environ.get("INGEST_LATE_GRACE_SECONDS", "5"))

    def accept(self, events):
        if not isinstance(events, list) or len(events) > 1000:
            raise ValueError("expected list of at most 1000 events")
        # Validate the complete request before changing dedup or accumulation state.
        validated = [self._validate_event(event) for event in events]
        with self.lock:
            for event in validated:
                identity = event["id"]
                if identity in self.seen:
                    continue
                if event["type"] == "observation":
                    try:
                        self.accumulator.add_observation(event["data"])
                    except ValueError as exc:
                        # Late data is explicitly dropped; never inserted into a newer bucket.
                        LOG.warning("dropping observation: %s", exc)
                elif event["type"] == "interval":
                    if len(self.intervals) >= self.interval_limit:
                        LOG.error("capture interval retry buffer full; dropping oldest interval")
                        self.intervals.popleft()
                    self.intervals.append(event["data"])
                self.seen[identity] = time.monotonic()
                while len(self.seen) > 100000:
                    self.seen.popitem(last=False)

    @staticmethod
    def _validate_event(event):
        if not isinstance(event, dict) or not isinstance(event.get("id"), (str, int)):
            raise ValueError("event must have a string or integer id")
        if not str(event["id"]):
            raise ValueError("event id cannot be empty")
        kind, data = event.get("type"), event.get("data")
        if not isinstance(data, dict):
            raise ValueError("event data must be an object")
        if kind == "observation":
            try:
                received_at = float(data["received_at"])
                frequency_hz = int(data["frequency_hz"])
                fields = data["fields"]
                for name in ("receiver_id", "address_type", "address"):
                    if not isinstance(data[name], str) or not data[name]:
                        raise ValueError(f"{name} must be a nonempty string")
                if not math.isfinite(received_at) or frequency_hz <= 0 or not isinstance(fields, dict):
                    raise ValueError("invalid observation timestamp, frequency, or fields")
                for item in fields.values():
                    if isinstance(item, dict) and "value" in item:
                        field_time = float(item.get("timestamp", received_at))
                        if not math.isfinite(field_time) or field_time > received_at:
                            raise ValueError("field timestamp must be finite and no later than received_at")
            except (KeyError, TypeError, OverflowError) as exc:
                raise ValueError("observation is missing valid normalized fields") from exc
        elif kind == "interval":
            try:
                if not isinstance(data["receiver_id"], str) or not data["receiver_id"]:
                    raise ValueError("interval receiver_id must be a nonempty string")
                if int(data["frequency_hz"]) <= 0:
                    raise ValueError("interval frequency_hz must be positive")
                start = float(data["started_at"])
                end = float(data["ended_at"]) if data.get("ended_at") is not None else None
                if not math.isfinite(start) or (end is not None and (not math.isfinite(end) or end < start)):
                    raise ValueError("invalid capture interval times")
            except (KeyError, TypeError, OverflowError) as exc:
                raise ValueError("interval is missing valid normalized fields") from exc
        else:
            raise ValueError("unknown event type")
        return {"id": str(event["id"]), "type": kind, "data": data}

    def persist(self, final=False):
        """Move finished snapshots and intervals into the spool (disk only, no network)."""
        with self.lock:
            rows = (
                self.accumulator.flush_all() if final else self.accumulator.flush_ready(time.time() - self.late_grace)
            )
            for row in rows:
                if len(self.snapshots) >= self.snapshot_limit:
                    LOG.error("snapshot retry buffer full; dropping oldest snapshot")
                    self.snapshots.popleft()
                self.snapshots.append(row)
            # Remove in-flight work while the lock is held. New requests cannot
            # shift deque positions while the spool write is in progress.
            snapshots = [self.snapshots.popleft() for _ in range(min(1000, len(self.snapshots)))]
            intervals = [self.intervals.popleft() for _ in range(min(100, len(self.intervals)))]
        written_at = datetime.now(UTC).isoformat()
        try:
            self.spool.put(SNAPSHOTS.name, [snapshot_row(row, written_at) for row in snapshots])
        except Exception as exc:
            with self.lock:
                self._restore_left(self.snapshots, snapshots, self.snapshot_limit, "snapshot")
                self._restore_left(self.intervals, intervals, self.interval_limit, "interval")
            LOG.warning("spool snapshot write deferred: %s", exc)
            return
        if self.heard is not None and snapshots:
            self.heard.update(snapshots)
        try:
            self.spool.put(INTERVALS.name, [interval_row(interval, written_at) for interval in intervals])
        except Exception as exc:
            with self.lock:
                self._restore_left(self.intervals, intervals, self.interval_limit, "interval")
            LOG.warning("spool interval write deferred: %s", exc)
        self._save_heard(final)

    def _save_heard(self, force: bool) -> None:
        if self.heard is None or not (force or time.monotonic() - self._heard_saved_at >= self.heard_save_seconds):
            return
        try:
            self.heard.prune()
            self.heard.save()
            self._heard_saved_at = time.monotonic()
        except Exception as exc:
            LOG.warning("saving %s failed: %s", self.heard.path, exc)

    def _restore_left(self, queue, items, limit, label):
        for item in reversed(items):
            queue.appendleft(item)
        while len(queue) > limit:
            queue.pop()
            LOG.error("%s retry buffer full during restore; dropping newest item", label)


class Sender:
    """Streams spooled batches to BigQuery and seeds ``heard.json`` once."""

    def __init__(
        self,
        spool: Spool,
        bigquery: BigQuery,
        heard: Heard | None = None,
        *,
        idle_seconds: float = 1,
        retry_seconds: float = 1,
        max_retry_seconds: float = 300,
        seed_retry_seconds: float = 300,
        report_seconds: float = 600,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.spool, self.bigquery, self.heard = spool, bigquery, heard
        self.idle_seconds, self.retry_seconds, self.max_retry_seconds = idle_seconds, retry_seconds, max_retry_seconds
        self.seed_retry_seconds, self.report_seconds, self.clock = seed_retry_seconds, report_seconds, clock
        self.delay = retry_seconds
        self.ready = False
        self.next_seed = clock()
        self.sent_since_report, self.last_report = 0, clock()

    def send(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.bigquery.stream(SPECS[table], rows)

    def step(self) -> float:
        """Send what is waiting; returns how long to wait before the next step."""
        try:
            if not self.ready:
                for spec in (SNAPSHOTS, INTERVALS):
                    self.bigquery.ensure_table(spec)
                self.ready = True
            self._seed_heard()
            self.sent_since_report += self.spool.drain(self.send, max_batches=50)
        except Exception as exc:
            delay, self.delay = self.delay, min(self.max_retry_seconds, self.delay * 2)
            LOG.warning("BigQuery send deferred %.0f s (%d batches waiting): %s", delay, len(self.spool.pending()), exc)
            return delay
        self.delay = self.retry_seconds
        if self.clock() - self.last_report >= self.report_seconds:
            LOG.info("sent %d rows in the last %.0f min", self.sent_since_report, self.report_seconds / 60)
            self.sent_since_report, self.last_report = 0, self.clock()
        return 0 if self.spool.pending() else self.idle_seconds

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            stop.wait(self.step())

    def _seed_heard(self) -> None:
        if self.heard is None or self.heard.seeded or self.clock() < self.next_seed:
            return
        try:
            count = self.heard.seed(self.bigquery.query(SEED_SQL, self.heard.seed_params()))
            LOG.info("seeded %s with %d aircraft from BigQuery", self.heard.path, count)
        except Exception as exc:
            self.next_seed = self.clock() + self.seed_retry_seconds
            LOG.warning("seeding %s deferred: %s", self.heard.path, exc)


def load_heard(path: str | os.PathLike) -> Heard:
    """Load ``heard.json``; an unreadable file is set aside and rebuilt by seeding."""
    try:
        return Heard.load(path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        bad = Path(path).with_name(Path(path).name + ".bad")
        Path(path).replace(bad)
        LOG.error("%s unreadable (%s); moved to %s and starting over", path, exc, bad.name)
        return Heard(path)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    spool = Spool(
        os.environ.get("SPOOL_DIR", "/var/lib/sky-scanner/spool"),
        max_bytes=int(os.environ.get("SPOOL_MAX_BYTES", DEFAULT_MAX_BYTES)),
    )
    heard = load_heard(os.environ.get("HEARD_PATH", "/var/lib/sky-scanner/heard/heard.json"))
    ingestor = Ingestor(spool, heard)
    sender = Sender(spool, BigQuery.from_env(os.environ), heard)
    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/events":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4194304:
                    raise ValueError("invalid request length")
                ingestor.accept(json.loads(self.rfile.read(length)))
                self.send_response(202)
                self.end_headers()
            except (ValueError, KeyError, TypeError) as exc:
                self.send_error(400, str(exc))

        def log_message(self, *_):
            pass

    server = HTTPServer(("0.0.0.0", 9000), Handler)
    server.timeout = 0.5

    def writer():
        while not stop.is_set():
            ingestor.persist()
            stop.wait(1)
        ingestor.persist(final=True)

    threads = [
        threading.Thread(target=writer, daemon=True),
        threading.Thread(target=sender.run, args=(stop,), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        while not stop.is_set():
            server.handle_request()
    finally:
        stop.set()
        # The writer's final flush lands in the spool; anything unsent goes next start.
        for thread in threads:
            thread.join(timeout=10)
        server.server_close()


if __name__ == "__main__":
    main()
