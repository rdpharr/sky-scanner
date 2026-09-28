import http.client
import json
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from urllib.parse import quote

from sky_scanner import web


class ParseDaysTests(unittest.TestCase):
    def test_empty_and_all_return_none(self):
        self.assertIsNone(web.parse_days(""))
        self.assertIsNone(web.parse_days("days=all"))

    def test_numeric_value(self):
        self.assertEqual(web.parse_days("days=7"), 7)

    def test_out_of_range_or_invalid_raises(self):
        with self.assertRaises(ValueError):
            web.parse_days("days=0")
        with self.assertRaises(ValueError):
            web.parse_days("days=9999")
        with self.assertRaises(ValueError):
            web.parse_days("days=x")


class ParseIdentityTests(unittest.TestCase):
    def test_normalizes_type_and_uppercases_address(self):
        self.assertEqual(web.parse_identity("icao", "a5bd0b"), ("icao", "A5BD0B"))

    def test_url_encoded_type_is_decoded(self):
        self.assertEqual(
            web.parse_identity(quote("readsb:adsr_icao", safe=""), "a5bd0b"),
            ("readsb:adsr_icao", "A5BD0B"),
        )

    def test_invalid_address_raises(self):
        with self.assertRaises(ValueError):
            web.parse_identity("icao", "ZZZZZZ")

    def test_too_long_address_raises(self):
        with self.assertRaises(ValueError):
            web.parse_identity("icao", "A5BD0B1234")

    def test_bad_type_characters_raise(self):
        with self.assertRaises(ValueError):
            web.parse_identity("icao!", "A5BD0B")


class ParseRangeTests(unittest.TestCase):
    def test_valid_range_with_offsets(self):
        query = ("start=" + quote("2024-01-01T00:00:00+00:00") +
                 "&end=" + quote("2024-01-01T01:00:00+00:00"))
        start, end = web.parse_range(query)
        self.assertEqual(start, datetime(2024, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2024, 1, 1, 1, tzinfo=timezone.utc))

    def test_missing_params_raise(self):
        with self.assertRaises(ValueError):
            web.parse_range("start=" + quote("2024-01-01T00:00:00+00:00"))
        with self.assertRaises(ValueError):
            web.parse_range("")

    def test_naive_datetime_raises(self):
        query = "start=2024-01-01T00:00:00&end=" + quote("2024-01-01T01:00:00+00:00")
        with self.assertRaises(ValueError):
            web.parse_range(query)

    def test_end_before_start_raises(self):
        query = ("start=" + quote("2024-01-02T00:00:00+00:00") +
                 "&end=" + quote("2024-01-01T00:00:00+00:00"))
        with self.assertRaises(ValueError):
            web.parse_range(query)

    def test_range_over_two_days_raises(self):
        query = ("start=" + quote("2024-01-01T00:00:00+00:00") +
                 "&end=" + quote("2024-01-04T00:00:00+00:00"))
        with self.assertRaises(ValueError):
            web.parse_range(query)


class FakeCursor:
    def __init__(self, summary_rows, totals_row):
        self.summary_rows = summary_rows
        self.totals_row = totals_row
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.summary_rows

    def fetchone(self):
        return self.totals_row

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class AircraftLogSummaryTests(unittest.TestCase):
    def _make_log(self, summary_rows=None, totals_row=None, fetch_types=None):
        summary_rows = summary_rows if summary_rows is not None else []
        totals_row = totals_row or (0, None, None)
        cursor = FakeCursor(summary_rows, totals_row)
        self.connect_calls = []

        def fake_connect(dsn):
            self.connect_calls.append(dsn)
            return FakeConnection(cursor)

        fetch_types = fetch_types or (lambda: {})
        log = web.AircraftLog("dsn://fake", connect=fake_connect, fetch_types=fetch_types)
        return log, cursor

    def test_payload_keys_and_since_none_for_all_time(self):
        totals_row = (42, datetime(2024, 1, 1, tzinfo=timezone.utc),
                      datetime(2024, 6, 1, tzinfo=timezone.utc))
        log, cursor = self._make_log(totals_row=totals_row)
        payload = log.summary(None)
        self.assertEqual(
            set(payload.keys()),
            {"generated_at", "days", "total_snapshots", "collecting_since",
             "last_heard", "classes", "rows"},
        )
        self.assertIsNone(payload["days"])
        self.assertEqual(payload["total_snapshots"], 42)
        self.assertEqual(payload["rows"], [])
        summary_call_params = cursor.executed[0][1]
        self.assertIsNone(summary_call_params["since"])

    def test_since_is_datetime_for_days(self):
        log, cursor = self._make_log()
        log.summary(7)
        since = cursor.executed[0][1]["since"]
        self.assertIsInstance(since, datetime)

    def test_second_call_within_cache_window_reuses_cache(self):
        log, _ = self._make_log()
        log.summary(7)
        log.summary(7)
        self.assertEqual(len(self.connect_calls), 1)

    def test_fetch_types_failure_does_not_break_summary_or_retry_soon(self):
        calls = []

        def failing_fetch():
            calls.append(1)
            raise RuntimeError("no network")

        log, _ = self._make_log(fetch_types=failing_fetch)
        payload1 = log.summary(None)
        self.assertEqual(payload1["rows"], [])
        self.assertEqual(len(calls), 1)

        # A different days value avoids the summary-level cache, but the
        # doc8643 type fetch itself should not be retried within 10 minutes.
        payload2 = log.summary(30)
        self.assertEqual(payload2["rows"], [])
        self.assertEqual(len(calls), 1)


class SequencedCursor:
    """A cursor whose fetchall/fetchone results are popped in call order."""

    def __init__(self, fetchall_results=None, fetchone_results=None):
        self.fetchall_results = list(fetchall_results or [])
        self.fetchone_results = list(fetchone_results or [])
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.fetchall_results.pop(0)

    def fetchone(self):
        return self.fetchone_results.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class AircraftLogAircraftTests(unittest.TestCase):
    def _make_log(self, cursor):
        def fake_connect(dsn):
            return FakeConnection(cursor)

        return web.AircraftLog("dsn://fake", connect=fake_connect, fetch_types=lambda: {})

    def test_empty_sightings_returns_empty_dict_without_further_queries(self):
        cursor = SequencedCursor(fetchall_results=[[]])
        log = self._make_log(cursor)
        result = log.aircraft("icao", "A5BD0B")
        self.assertEqual(result, {})
        # Only the sightings query should have run.
        self.assertEqual(len(cursor.executed), 1)

    def test_payload_includes_address_fields(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 1, 1, 0, tzinfo=timezone.utc)
        sighting_row = (start, end, 3, 30, [1_090_000_000], ["UAL123"], ["1200"],
                        30000, 35000, 450, 3, False)
        cursor = SequencedCursor(
            fetchall_results=[[sighting_row], []],
            fetchone_results=[None],
        )
        log = self._make_log(cursor)
        result = log.aircraft("icao", "A5BD0B")
        self.assertEqual(result["address"], "A5BD0B")
        self.assertEqual(result["address_type"], "icao")
        self.assertEqual(result["sighting_gap_minutes"], web.aircraft_log.SIGHTING_GAP_MINUTES)
        self.assertEqual(result["totals"]["sightings"], 1)


class AircraftLogSnapshotsTests(unittest.TestCase):
    def _make_log(self, rows):
        cursor = SequencedCursor(fetchall_results=[rows])

        def fake_connect(dsn):
            return FakeConnection(cursor)

        return web.AircraftLog("dsn://fake", connect=fake_connect, fetch_types=lambda: {})

    def _row(self):
        bucket = datetime(2024, 1, 1, tzinfo=timezone.utc)
        return (bucket, 1_090_000_000, bucket, bucket, 1, "", None, None, None, None,
                None, None, None, None, None, False, {})

    def test_returns_rows_and_not_truncated(self):
        log = self._make_log([self._row()])
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 2, tzinfo=timezone.utc)
        result = log.snapshots("icao", "A5BD0B", start, end)
        self.assertEqual(len(result["rows"]), 1)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["start"], start.isoformat())
        self.assertEqual(result["end"], end.isoformat())

    def test_truncated_when_5000_rows(self):
        log = self._make_log([self._row() for _ in range(5000)])
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 2, tzinfo=timezone.utc)
        result = log.snapshots("icao", "A5BD0B", start, end)
        self.assertEqual(len(result["rows"]), 5000)
        self.assertTrue(result["truncated"])


class AircraftPathHandlerTests(unittest.TestCase):
    class FakeLog:
        def __init__(self):
            self.aircraft_result = {}
            self.snapshots_result = {}
            self.raise_error = False
            self.snapshots_call = None

        def aircraft(self, address_type, address):
            if self.raise_error:
                raise RuntimeError("db exploded")
            return self.aircraft_result

        def snapshots(self, address_type, address, start, end):
            if self.raise_error:
                raise RuntimeError("db exploded")
            self.snapshots_call = (address_type, address, start, end)
            return self.snapshots_result

    def setUp(self):
        self.fake_log = self.FakeLog()
        handler_cls = web.make_handler(self.fake_log)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _connection(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)

    def test_aircraft_found_returns_200(self):
        self.fake_log.aircraft_result = {"address": "A5BD0B"}
        conn = self._connection()
        conn.request("GET", "/api/aircraft/icao/A5BD0B")
        resp = conn.getresponse()
        body = resp.read()
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(body), {"address": "A5BD0B"})
        conn.close()

    def test_aircraft_not_found_returns_404(self):
        self.fake_log.aircraft_result = {}
        conn = self._connection()
        conn.request("GET", "/api/aircraft/icao/A5BD0B")
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 404)
        conn.close()

    def test_invalid_address_returns_400(self):
        conn = self._connection()
        conn.request("GET", "/api/aircraft/icao/ZZZZZZ")
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 400)
        conn.close()

    def test_snapshots_returns_200_with_parsed_datetimes(self):
        self.fake_log.snapshots_result = {"rows": [], "truncated": False}
        start = quote("2024-01-01T00:00:00+00:00", safe="")
        end = quote("2024-01-01T01:00:00+00:00", safe="")
        conn = self._connection()
        conn.request("GET", f"/api/aircraft/icao/A5BD0B/snapshots?start={start}&end={end}")
        resp = conn.getresponse()
        body = resp.read()
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(body), {"rows": [], "truncated": False})
        address_type, address, parsed_start, parsed_end = self.fake_log.snapshots_call
        self.assertEqual((address_type, address), ("icao", "A5BD0B"))
        self.assertEqual(parsed_start, datetime(2024, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(parsed_end, datetime(2024, 1, 1, 1, tzinfo=timezone.utc))
        conn.close()

    def test_snapshots_bad_range_returns_400(self):
        conn = self._connection()
        conn.request("GET", "/api/aircraft/icao/A5BD0B/snapshots?start=bad&end=bad")
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 400)
        conn.close()

    def test_aircraft_failure_returns_503_without_leaking_detail(self):
        self.fake_log.raise_error = True
        conn = self._connection()
        conn.request("GET", "/api/aircraft/icao/A5BD0B")
        resp = conn.getresponse()
        body = resp.read()
        self.assertEqual(resp.status, 503)
        self.assertNotIn(b"db exploded", body)
        conn.close()


class MakeHandlerTests(unittest.TestCase):
    class FakeLog:
        def __init__(self):
            self.raise_error = False
            self.last_days = "unset"

        def summary(self, days):
            if self.raise_error:
                raise RuntimeError("db exploded")
            self.last_days = days
            return {"days": days, "rows": []}

    def setUp(self):
        self.fake_log = self.FakeLog()
        handler_cls = web.make_handler(self.fake_log)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _connection(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)

    def test_index_page(self):
        conn = self._connection()
        conn.request("GET", "/")
        resp = conn.getresponse()
        body = resp.read()
        self.assertEqual(resp.status, 200)
        self.assertIn("text/html", resp.getheader("Content-Type"))
        self.assertIn(b"Sky Scanner Aircraft Log", body)
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        conn.close()

    def test_api_aircraft_returns_json(self):
        conn = self._connection()
        conn.request("GET", "/api/aircraft?days=7")
        resp = conn.getresponse()
        body = resp.read()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "application/json")
        self.assertEqual(json.loads(body), {"days": 7, "rows": []})
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        conn.close()

    def test_api_aircraft_bad_days_returns_400(self):
        conn = self._connection()
        conn.request("GET", "/api/aircraft?days=bad")
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 400)
        conn.close()

    def test_api_aircraft_failure_returns_503_without_leaking_detail(self):
        self.fake_log.raise_error = True
        conn = self._connection()
        conn.request("GET", "/api/aircraft")
        resp = conn.getresponse()
        body = resp.read()
        self.assertEqual(resp.status, 503)
        self.assertNotIn(b"db exploded", body)
        conn.close()

    def test_healthz(self):
        conn = self._connection()
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        body = resp.read()
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, b"ok\n")
        conn.close()

    def test_unknown_path_404(self):
        conn = self._connection()
        conn.request("GET", "/nope")
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 404)
        conn.close()

    def test_post_not_allowed(self):
        conn = self._connection()
        conn.request("POST", "/api/aircraft")
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 501)
        conn.close()


if __name__ == "__main__":
    unittest.main()
