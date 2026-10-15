"""Read-only LAN aircraft log: one HTML page plus a JSON endpoint.

Connects with the read-only ``explorer`` role (``WEB_DATABASE_URL``). Serves
``GET /`` (the page), ``GET /api/aircraft?days=N`` (summary JSON; omit
``days`` for all time), ``GET /api/aircraft/<type>/<address>`` (one aircraft:
registry versions, sightings, latest decoded fields) and
``.../snapshots?start=&end=`` (snapshots in a range of at most two days).
Summaries are cached briefly so repeated refreshes do not rescan the
snapshot table. There are no write endpoints.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import aircraft_log, military

LOG = logging.getLogger(__name__)
PAGE = Path(__file__).with_name("aircraft_log.html")
CACHE_SECONDS = 20


class AircraftLog:
    def __init__(self, dsn: str, connect=None, fetch_types=aircraft_log.fetch_doc8643):
        self.dsn = dsn
        self._connect = connect
        self._fetch_types = fetch_types
        self._types: dict | None = None
        self._types_retry_at = 0.0
        self._cache: dict[int | None, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def connect(self):
        if self._connect:
            return self._connect(self.dsn)
        import psycopg
        return psycopg.connect(self.dsn, connect_timeout=10)

    def types(self) -> dict:
        # Doc 8643 is only used for classification; retry at most every 10 minutes.
        if self._types is None and time.monotonic() >= self._types_retry_at:
            try:
                self._types = self._fetch_types()
            except Exception as exc:
                LOG.warning("ICAO type list unavailable: %s", exc)
                self._types_retry_at = time.monotonic() + 600
        return self._types or {}

    def summary(self, days: int | None) -> dict:
        with self._lock:
            cached = self._cache.get(days)
            if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
                return cached[1]
            since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
            with self.connect() as connection, connection.cursor() as cursor:
                cursor.execute(aircraft_log.SUMMARY_SQL, {"since": since})
                rows = cursor.fetchall()
                cursor.execute(aircraft_log.TOTALS_SQL)
                snapshots, first_bucket, last_heard = cursor.fetchone()
            payload = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "days": days,
                "total_snapshots": snapshots,
                "collecting_since": first_bucket.isoformat() if first_bucket else None,
                "last_heard": last_heard.isoformat() if last_heard else None,
                "classes": aircraft_log.CLASS_ORDER,
                "rows": aircraft_log.summarize(rows, self.types()),
            }
            self._cache[days] = (time.monotonic(), payload)
            return payload

    def aircraft(self, address_type: str, address: str) -> dict:
        params = {"address_type": address_type, "address": address}
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(aircraft_log.SIGHTINGS_SQL, params)
            sightings = cursor.fetchall()
            if not sightings:
                return {}
            cursor.execute(aircraft_log.METADATA_SQL, params)
            metadata = cursor.fetchall()
            cursor.execute(aircraft_log.LATEST_SNAPSHOT_SQL, params)
            latest = cursor.fetchone()
            cursor.execute("SELECT code, name FROM military_operator_codes")
            codes = dict(cursor.fetchall())
            words = sorted({w for row in sightings for c in (row[5] or [])
                            if (w := military.callword(c, set(codes)))})
            cursor.execute("SELECT word, status, source_url, fetched_at, data FROM callword_lookups "
                           "WHERE word = ANY(%(words)s) ORDER BY word", {"words": words})
            callwords = cursor.fetchall()
            payload = aircraft_log.detail(address, metadata, sightings, latest, self.types(),
                                          codes, callwords)
            payload["type_image"] = None
            if payload["identity"]["tc"]:
                cursor.execute(
                    "SELECT title, description, article_url, image_url, file_url, method, fetched_at "
                    "FROM aircraft_type_images WHERE designator = %(tc)s AND status = 'found'",
                    {"tc": payload["identity"]["tc"]})
                image = cursor.fetchone()
                if image:
                    keys = ("title", "description", "article_url", "image_url", "file_url",
                            "method", "fetched_at")
                    payload["type_image"] = dict(zip(keys, image))
                    payload["type_image"]["fetched_at"] = aircraft_log._iso(image[6])
        payload.update(address=address, address_type=address_type,
                       sighting_gap_minutes=aircraft_log.SIGHTING_GAP_MINUTES)
        return payload

    def snapshots(self, address_type: str, address: str, start: datetime, end: datetime) -> dict:
        params = {"address_type": address_type, "address": address, "start": start, "end": end}
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(aircraft_log.SNAPSHOTS_SQL, params)
            rows = aircraft_log.snapshot_rows(cursor.fetchall())
        return {"start": start.isoformat(), "end": end.isoformat(), "rows": rows,
                "truncated": len(rows) >= 5000}


ADDRESS_TYPE = re.compile(r"^[a-z0-9_:]{1,40}$")
ADDRESS = re.compile(r"^[0-9A-F]{6}$")
AIRCRAFT_PATH = re.compile(r"^/api/aircraft/([^/]+)/([^/]+)(/snapshots)?$")


def parse_identity(address_type: str, address: str) -> tuple[str, str]:
    address_type, address = unquote(address_type), unquote(address).upper()
    if not ADDRESS_TYPE.match(address_type) or not ADDRESS.match(address):
        raise ValueError("invalid aircraft identity")
    return address_type, address


def parse_range(query: str) -> tuple[datetime, datetime]:
    values = parse_qs(query)
    try:
        start = datetime.fromisoformat(values["start"][0])
        end = datetime.fromisoformat(values["end"][0])
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError("start and end must be ISO timestamps") from exc
    if start.tzinfo is None or end.tzinfo is None or end < start:
        raise ValueError("start and end must be timezone-aware with end >= start")
    if end - start > timedelta(days=2):
        raise ValueError("range is limited to 2 days")
    return start, end


def parse_days(query: str) -> int | None:
    value = parse_qs(query).get("days", [""])[0]
    if value in ("", "all"):
        return None
    days = int(value)
    if not 1 <= days <= 3650:
        raise ValueError("days must be between 1 and 3650")
    return days


def make_handler(log: AircraftLog):
    page = PAGE.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "SkyScannerLog/1"

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                             "font-src https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; "
                             "img-src 'self' https://upload.wikimedia.org https://thumb.wikimedia.org")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                self._send(200, page, "text/html; charset=utf-8")
            elif url.path == "/api/aircraft":
                try:
                    days = parse_days(url.query)
                except ValueError as exc:
                    self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
                    return
                try:
                    body = json.dumps(log.summary(days), separators=(",", ":")).encode()
                except Exception as exc:
                    LOG.warning("aircraft summary failed: %s", exc)
                    self._send(503, json.dumps({"error": "database unavailable"}).encode(), "application/json")
                    return
                self._send(200, body, "application/json")
            elif AIRCRAFT_PATH.match(url.path):
                match = AIRCRAFT_PATH.match(url.path)
                try:
                    address_type, address = parse_identity(match.group(1), match.group(2))
                    span = parse_range(url.query) if match.group(3) else None
                except ValueError as exc:
                    self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
                    return
                try:
                    payload = (log.snapshots(address_type, address, *span) if span
                               else log.aircraft(address_type, address))
                except Exception as exc:
                    LOG.warning("aircraft detail failed: %s", exc)
                    self._send(503, json.dumps({"error": "database unavailable"}).encode(), "application/json")
                    return
                if not payload:
                    self._send(404, json.dumps({"error": "aircraft not seen"}).encode(), "application/json")
                    return
                self._send(200, json.dumps(payload, separators=(",", ":")).encode(), "application/json")
            elif url.path == "/healthz":
                self._send(200, b"ok\n", "text/plain")
            else:
                self._send(404, b"not found\n", "text/plain")

        def log_message(self, *_):
            pass

    return Handler


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    dsn = os.environ.get("WEB_DATABASE_URL")
    if not dsn:
        raise SystemExit("WEB_DATABASE_URL is not configured")
    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("WEB_PORT", "8080"))),
                                 make_handler(AircraftLog(dsn)))
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    LOG.info("aircraft log listening on port %s", server.server_address[1])
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
