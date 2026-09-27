import http.client
import json
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

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
