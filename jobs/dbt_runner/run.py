"""Run dbt on a schedule and report every problem as a structured log line.

Usage: run.py hourly | daily | freshness

- hourly:    dbt build, except models tagged "daily" (and their tests)
- daily:     dbt build of everything
- freshness: dbt source freshness

Each failed or erroring node (model, test, seed, stale source) is printed as
one JSON line with severity ERROR and event "dbt_node_failed"; warnings get
severity WARNING and event "dbt_node_warned". Cloud Run sends stdout to Cloud
Logging, where log-based alert policies email the ERROR lines. Every result
is also loaded into BigQuery (analytics.dbt_invocations and
analytics.dbt_node_results) for the Data health page.

The exit code is non-zero only when dbt could not run at all (exit code 2)
or produced no results: failing tests are reported through the logs, not by
failing the job, so one problem raises one alert.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

DBT_DIR = Path(os.environ.get("DBT_PROJECT_DIR", "/dbt"))
COMMANDS = {
    "hourly": ["build", "--exclude", "tag:daily"],
    "daily": ["build"],
    "freshness": ["source", "freshness"],
}
PROBLEM = {"error": "ERROR", "fail": "ERROR", "runtime error": "ERROR", "warn": "WARNING"}

INVOCATION_SCHEMA = [
    ("invocation_id", "STRING"),
    ("mode", "STRING"),
    ("command", "STRING"),
    ("started_at", "TIMESTAMP"),
    ("finished_at", "TIMESTAMP"),
    ("dbt_exit_code", "INT64"),
    ("nodes", "INT64"),
    ("errors", "INT64"),
    ("warnings", "INT64"),
    ("bytes_billed", "INT64"),
    ("execution_environment", "STRING"),
]
NODE_SCHEMA = [
    ("invocation_id", "STRING"),
    ("mode", "STRING"),
    ("finished_at", "TIMESTAMP"),
    ("unique_id", "STRING"),
    ("resource_type", "STRING"),
    ("name", "STRING"),
    ("status", "STRING"),
    ("failures", "INT64"),
    ("message", "STRING"),
    ("execution_time_s", "FLOAT64"),
    ("bytes_billed", "INT64"),
    ("max_loaded_at", "TIMESTAMP"),
    ("age_seconds", "FLOAT64"),
]


def log(severity: str, message: str, **fields) -> None:
    print(json.dumps({"severity": severity, "message": message, **fields}, default=str), flush=True)


def node_rows(mode: str, results: dict, finished_at: str) -> list[dict]:
    """Normalize run_results.json or sources.json into one row per node."""
    rows = []
    for r in results.get("results", []):
        unique_id = r["unique_id"]
        parts = unique_id.split(".")
        resource_type = parts[0]
        # test.<project>.<name>.<hash>; source.<project>.<source>.<table>; model.<project>.<name>
        if resource_type == "source":
            name = ".".join(parts[-2:])
        else:
            name = parts[2] if len(parts) > 2 else parts[-1]
        adapter = r.get("adapter_response") or {}
        rows.append(
            {
                "invocation_id": results.get("metadata", {}).get("invocation_id"),
                "mode": mode,
                "finished_at": finished_at,
                "unique_id": unique_id,
                "resource_type": resource_type,
                "name": name,
                "status": r.get("status"),
                "failures": r.get("failures"),
                "message": r.get("message") or r.get("error"),
                "execution_time_s": r.get("execution_time"),
                "bytes_billed": adapter.get("bytes_billed"),
                "max_loaded_at": r.get("max_loaded_at"),
                "age_seconds": r.get("max_loaded_at_time_ago_in_s"),
            }
        )
    return rows


def definitions(manifest: dict, exported_at: str) -> list[dict]:
    """Metric, model and column definitions from dbt's manifest, for the
    website's Definitions page and the MCP server."""
    rows = []
    for metric in manifest.get("metrics", {}).values():
        params = metric.get("type_params") or {}
        if metric.get("type") == "ratio":
            formula = f"{params['numerator']['name']} / {params['denominator']['name']}"
        else:
            formula = (params.get("measure") or {}).get("name")
        where = (metric.get("filter") or {}).get("where_filters") or []
        rows.append(
            {
                "kind": "metric",
                "name": metric["name"],
                "label": metric.get("label"),
                "description": metric.get("description"),
                "metric_type": metric.get("type"),
                "formula": formula,
                "filter": " and ".join(f["where_sql_template"] for f in where) or None,
                "exported_at": exported_at,
            }
        )
    for node in manifest.get("nodes", {}).values():
        if node.get("resource_type") != "model":
            continue
        # The logical dataset (public, analytics), whichever target built it.
        dataset = (node.get("config") or {}).get("schema") or "analytics"
        rows.append(
            {
                "kind": "model",
                "name": node["name"],
                "model": node["name"],
                "dataset": dataset,
                "description": node.get("description"),
                "exported_at": exported_at,
            }
        )
        for column in node.get("columns", {}).values():
            rows.append(
                {
                    "kind": "column",
                    "name": column["name"],
                    "model": node["name"],
                    "dataset": dataset,
                    "description": column.get("description"),
                    "data_type": column.get("data_type"),
                    "exported_at": exported_at,
                }
            )
    return rows


def report(rows: list[dict]) -> tuple[int, int]:
    errors = warnings = 0
    for row in rows:
        severity = PROBLEM.get(row["status"])
        if not severity:
            continue
        if severity == "ERROR":
            errors += 1
        else:
            warnings += 1
        if row["resource_type"] == "source":
            detail = f"newest row is {round((row['age_seconds'] or 0) / 60)} min old"
        elif row["failures"]:
            detail = f"{row['failures']} failing rows"
        else:
            detail = (row["message"] or "")[:500]
        log(
            severity,
            f"dbt {row['resource_type']} {row['name']} {row['status']}: {detail}",
            event="dbt_node_failed" if severity == "ERROR" else "dbt_node_warned",
            **{
                k: row[k] for k in ("mode", "resource_type", "name", "status", "failures", "unique_id", "invocation_id")
            },
        )
    return errors, warnings


DEFINITION_SCHEMA = [
    ("kind", "STRING"),
    ("name", "STRING"),
    ("model", "STRING"),
    ("dataset", "STRING"),
    ("label", "STRING"),
    ("description", "STRING"),
    ("metric_type", "STRING"),
    ("formula", "STRING"),
    ("filter", "STRING"),
    ("data_type", "STRING"),
    ("exported_at", "TIMESTAMP"),
]
TABLES = {
    "dbt_invocations": (INVOCATION_SCHEMA, "started_at"),
    "dbt_node_results": (NODE_SCHEMA, "finished_at"),
    "dbt_definitions": (DEFINITION_SCHEMA, None),
}


def ensure_tables(client, dataset: str) -> None:
    """Create the results tables if missing: dbt models read them as sources."""
    from google.cloud import bigquery

    for name, (schema, partition) in TABLES.items():
        table = bigquery.Table(
            f"{client.project}.{dataset}.{name}", schema=[bigquery.SchemaField(n, t) for n, t in schema]
        )
        if partition:
            table.time_partitioning = bigquery.TimePartitioning(field=partition)
        client.create_table(table, exists_ok=True)


def save(client, table: str, rows: list[dict], replace: bool = False) -> None:
    from google.cloud import bigquery

    if not rows:
        return
    schema, partition = TABLES[table.split(".")[-1]]
    config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=[bigquery.SchemaField(n, t) for n, t in schema],
        write_disposition="WRITE_TRUNCATE" if replace else "WRITE_APPEND",
    )
    if partition:
        config.time_partitioning = bigquery.TimePartitioning(field=partition)
    with tempfile.TemporaryFile() as f:
        for row in rows:
            f.write(json.dumps({n: row.get(n) for n, _ in schema}, default=str).encode() + b"\n")
        f.seek(0)
        client.load_table_from_file(f, table, job_config=config).result()


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "hourly"
    if mode not in COMMANDS:
        log("ERROR", f"unknown mode {mode!r}; expected one of {sorted(COMMANDS)}")
        return 2
    command = ["dbt", *COMMANDS[mode], "--profiles-dir", str(DBT_DIR), "--project-dir", str(DBT_DIR)]
    client = dataset = None
    try:
        from google.cloud import bigquery

        client = bigquery.Client(project=os.environ.get("GCP_PROJECT", "sky-scanner-pharr"))
        dataset = os.environ.get("OPS_DATASET", "analytics")
        ensure_tables(client, dataset)
    except Exception as exc:
        log("WARNING", f"could not prepare dbt results tables: {exc}", event="dbt_results_not_saved")
    started_at = datetime.now(UTC).isoformat()
    log("INFO", f"running dbt {' '.join(COMMANDS[mode])}", event="dbt_started", mode=mode)
    rc = subprocess.run(command, cwd=DBT_DIR).returncode
    finished_at = datetime.now(UTC).isoformat()

    artifact = DBT_DIR / "target" / ("sources.json" if mode == "freshness" else "run_results.json")
    if rc == 2 or not artifact.exists():
        log("ERROR", f"dbt {mode} could not run (exit code {rc})", event="dbt_crashed", mode=mode)
        return 1
    results = json.loads(artifact.read_text())
    rows = node_rows(mode, results, finished_at)
    errors, warnings = report(rows)
    invocation = {
        "invocation_id": results.get("metadata", {}).get("invocation_id"),
        "mode": mode,
        "command": " ".join(COMMANDS[mode]),
        "started_at": started_at,
        "finished_at": finished_at,
        "dbt_exit_code": rc,
        "nodes": len(rows),
        "errors": errors,
        "warnings": warnings,
        "bytes_billed": sum(r["bytes_billed"] or 0 for r in rows),
        "execution_environment": os.environ.get("CLOUD_RUN_JOB", "local"),
    }
    log(
        "INFO",
        f"dbt {mode} finished: {len(rows)} nodes, {errors} errors, {warnings} warnings",
        event="dbt_finished",
        **{k: invocation[k] for k in ("mode", "nodes", "errors", "warnings", "bytes_billed", "invocation_id")},
    )
    try:
        save(client, f"{client.project}.{dataset}.dbt_invocations", [invocation])
        save(client, f"{client.project}.{dataset}.dbt_node_results", rows)
        manifest = DBT_DIR / "target" / "manifest.json"
        if mode != "freshness" and manifest.exists():
            save(
                client,
                f"{client.project}.{dataset}.dbt_definitions",
                definitions(json.loads(manifest.read_text()), finished_at),
                replace=True,
            )
    except Exception as exc:  # results are a convenience; alerts already went out
        log("WARNING", f"could not save dbt results to BigQuery: {exc}", event="dbt_results_not_saved")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
