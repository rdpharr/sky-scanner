"""Private Compose-network ingestion endpoint with bounded, retryable DB writes."""

from __future__ import annotations

import json
import logging
import math
import os
import signal
import threading
import time
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, HTTPServer

from .snapshots import SnapshotAccumulator
from .storage import PostgresStorage

LOG = logging.getLogger(__name__)


class Ingestor:
    def __init__(self):
        self.accumulator = SnapshotAccumulator()
        self.lock = threading.Lock()
        self.seen = OrderedDict()
        self.snapshots = deque()
        self.snapshot_limit = 20000
        self.intervals = deque()
        self.interval_limit = 10000
        self.storage = None
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
            # shift deque positions while the DB call is in progress.
            snapshots = [self.snapshots.popleft() for _ in range(min(1000, len(self.snapshots)))]
            intervals = [self.intervals.popleft() for _ in range(min(100, len(self.intervals)))]
        try:
            if self.storage is None:
                self.storage = PostgresStorage.from_env()
                self.storage.init_schema()
                self.storage.register_receiver(os.environ.get("RECEIVER_ID", "home-hackrf"))
        except Exception as exc:
            with self.lock:
                self._restore_left(self.snapshots, snapshots, self.snapshot_limit, "snapshot")
                self._restore_left(self.intervals, intervals, self.interval_limit, "interval")
            LOG.warning("database initialization deferred: %s", exc)
            self._reset_storage()
            return
        try:
            if snapshots:
                self.storage.save_snapshots(snapshots)
        except Exception as exc:
            with self.lock:
                self._restore_left(self.snapshots, snapshots, self.snapshot_limit, "snapshot")
                self._restore_left(self.intervals, intervals, self.interval_limit, "interval")
            LOG.warning("database snapshot write deferred: %s", exc)
            self._reset_storage()
            return
        try:
            for interval in intervals:
                self.storage.save_capture_interval(interval)
        except Exception as exc:
            with self.lock:
                self._restore_left(self.intervals, intervals, self.interval_limit, "interval")
            LOG.warning("database write deferred: %s", exc)
            self._reset_storage()

    def _restore_left(self, queue, items, limit, label):
        for item in reversed(items):
            queue.appendleft(item)
        while len(queue) > limit:
            queue.pop()
            LOG.error("%s retry buffer full during restore; dropping newest item", label)

    def _reset_storage(self):
        if self.storage:
            try:
                self.storage.connection.close()
            except Exception:
                LOG.debug("error closing failed database connection", exc_info=True)
        self.storage = None


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ingestor = Ingestor()
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

    worker = threading.Thread(target=writer, daemon=True)
    worker.start()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        while not stop.is_set():
            server.handle_request()
    finally:
        stop.set()
        worker.join(timeout=10)
        server.server_close()


if __name__ == "__main__":
    main()
