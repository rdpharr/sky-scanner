import os
import sys
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("GCP_PROJECT", "sky-scanner-pharr")
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

import server  # noqa: E402


def ref(project, dataset, table="t"):
    return SimpleNamespace(project=project, dataset_id=dataset, table_id=table)


class DryRunTests(unittest.TestCase):
    def test_allows_a_select_on_public_views(self):
        # A view's dry run lists the private tables underneath it; that is fine.
        server.check_dry_run("SELECT", [ref("sky-scanner-pharr", "analytics", "dim_aircraft")], 10)

    def test_refuses_statements_other_than_select(self):
        for statement in ("DELETE", "CREATE_TABLE_AS_SELECT", "SCRIPT"):
            with self.assertRaises(ToolError):
                server.check_dry_run(statement, [], 0)

    def test_refuses_other_projects(self):
        with self.assertRaisesRegex(ToolError, "bigquery-public-data.samples"):
            server.check_dry_run("SELECT", [ref("bigquery-public-data", "samples")], 10)

    def test_refuses_large_scans(self):
        with self.assertRaisesRegex(ToolError, "limit"):
            server.check_dry_run("SELECT", [], server.SQL_MAX_BYTES + 1)


class HelperTests(unittest.TestCase):
    def test_jsonable(self):
        t = datetime(2026, 10, 16, tzinfo=UTC)
        self.assertEqual(
            server.jsonable({"t": t, "n": Decimal("1.5"), "l": [t]}),
            {"t": "2026-10-16T00:00:00+00:00", "n": 1.5, "l": ["2026-10-16T00:00:00+00:00"]},
        )

    def test_rounds_measurements_but_not_coordinates(self):
        self.assertEqual(
            server.jsonable(
                {
                    "kt": 454.881866,
                    "alt": 27000.0,
                    "nm": 8.68890993,
                    "ratio": 1.0000152,
                    "latitude": 36.3456,
                    "longitude": -115.3512,
                }
            ),
            {"kt": 455, "alt": 27000, "nm": 8.7, "ratio": 1.0, "latitude": 36.3456, "longitude": -115.3512},
        )

    def test_clamp_bounds_tool_arguments(self):
        self.assertEqual(server.clamp(10_000, 1, 365), 365)
        self.assertEqual(server.clamp(-5, 1, 365), 1)

    def test_rate_limit(self):
        with patch.object(server, "CALLS_PER_MINUTE", 3):
            server._calls.clear()
            for _ in range(3):
                server.rate_limit()
            with self.assertRaises(ToolError):
                server.rate_limit()
        server._calls.clear()


class DateRangeTests(unittest.TestCase):
    def test_defaults_to_the_last_seven_complete_days(self):
        start, end = server.date_range("", "")
        from datetime import date

        self.assertEqual((date.fromisoformat(end) - date.fromisoformat(start)).days, 7)

    def test_end_date_is_inclusive(self):
        # "26 September to 2 October" covers 7 days, ending before 3 October.
        self.assertEqual(server.date_range("2026-09-26", "2026-10-02"), ("2026-09-26", "2026-10-03"))
        self.assertEqual(server.date_range("2026-10-02", "2026-10-02"), ("2026-10-02", "2026-10-03"))

    def test_rejects_bad_ranges(self):
        for start, end in (("2026-10-03", "2026-09-26"), ("2026-01-01", "2026-10-01"), ("yesterday", "")):
            with self.assertRaises(ToolError):
                server.date_range(start, end)


class ToolTests(unittest.TestCase):
    def test_every_tool_is_read_only(self):
        import asyncio

        tools = asyncio.run(server.mcp.list_tools())
        self.assertGreaterEqual(len(tools), 19)
        for tool in tools:
            self.assertTrue(tool.annotations.read_only_hint, tool.name)
            self.assertFalse(tool.annotations.destructive_hint, tool.name)
            self.assertTrue(tool.description, tool.name)

    def test_run_sql_requires_the_token(self):
        ctx = SimpleNamespace(headers={"x-sky-scanner-token": "wrong"})
        with patch.object(server, "SQL_TOKEN", "right"), patch.object(server, "client") as client:
            with self.assertRaisesRegex(ToolError, "X-Sky-Scanner-Token"):
                server.run_sql("select 1", ctx)
            client.assert_not_called()

    def test_run_sql_is_disabled_without_a_configured_token(self):
        ctx = SimpleNamespace(headers={"x-sky-scanner-token": ""})
        with patch.object(server, "SQL_TOKEN", ""), self.assertRaises(ToolError):
            server.run_sql("select 1", ctx)


class HttpTests(unittest.TestCase):
    def test_refuses_the_get_stream_but_answers_posts(self):
        from starlette.testclient import TestClient

        headers = {"Accept": "application/json, text/event-stream"}
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        }
        with TestClient(server.http_app()) as http:
            self.assertEqual(http.get("/mcp", headers=headers).status_code, 405)
            response = http.post("/mcp", headers=headers, json=initialize)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["result"]["serverInfo"]["name"], "sky-scanner")
            self.assertEqual(http.get("/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()
