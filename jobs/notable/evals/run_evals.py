"""Evaluate the agent: Q&A accuracy and notable-edition recall.

Usage (from jobs/notable):
  python -m evals.run_evals qa --model xiaomi/mimo-v2.6-pro [--model other/model]
  python -m evals.run_evals recall [--edition-id ID]

qa: each question in qa_questions.json is answered by an agent working only
through the MCP server, then graded against the question's gold SQL, run
directly on the public BigQuery views. The questions are about a fixed past
week, so answers do not drift.

recall: share of the items in golden_week.json (the owner's hand-checked
report for 26 Sep to 2 Oct 2026) that an edition for the same week covers.

Results are stored in ai.eval_runs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from google.cloud import bigquery
from mcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent  # noqa: E402
import mcp_bridge  # noqa: E402
import openrouter  # noqa: E402

HERE = Path(__file__).parent
PROJECT = os.environ.get("GCP_PROJECT", "sky-scanner-pharr")
MCP_URL = os.environ.get("MCP_URL", "https://mcp.skyscanner.pharrhomelab.com/mcp")
MAX_CALLS_PER_QUESTION = 12
CONCURRENCY = 4
RUN_SCHEMA = [
    ("run_id", "STRING"),
    ("kind", "STRING"),
    ("model", "STRING"),
    ("started_at", "TIMESTAMP"),
    ("score", "FLOAT64"),
    ("passed", "INT64"),
    ("total", "INT64"),
    ("cost_usd", "FLOAT64"),
    ("seconds", "FLOAT64"),
    ("details", "JSON"),
]

QA_SYSTEM = (
    "You answer questions about aircraft data using only the tools. Call tools to find the answer, "
    'then reply with only JSON: {"answer": <number or short text>, "how": "one sentence on which '
    'tool and field gave it"}. Give numbers as plain numbers without units.'
)


def bq() -> bigquery.Client:
    return bigquery.Client(project=PROJECT)


def grade(kind: str, answer: Any, gold: Any, tolerance: float = 0.0) -> bool:
    if answer is None or gold is None:
        return False
    if kind == "number":
        try:
            a = float(re.sub(r"[^\d.\-]", "", str(answer)))
        except ValueError:
            return False
        g = float(gold)
        return abs(a - g) <= max(0.5, abs(g) * tolerance)
    a, g = str(answer).strip().casefold(), str(gold).strip().casefold()
    return a == g or g in a


async def ask(model: str, question: dict[str, Any], gold: Any, semaphore: asyncio.Semaphore) -> dict[str, Any]:
    async with semaphore:
        run = agent.Run(model=model)
        started = time.monotonic()
        answer, error = None, None
        try:
            async with Client(MCP_URL) as client:
                tools = mcp_bridge.openai_tools((await client.list_tools()).tools)
                messages: list[dict] = [
                    {"role": "system", "content": QA_SYSTEM},
                    {"role": "user", "content": question["q"]},
                ]
                while True:
                    final = len(run.trace) >= MAX_CALLS_PER_QUESTION
                    if final:
                        messages.append({"role": "user", "content": "Answer now with the JSON."})
                    response = await asyncio.to_thread(
                        openrouter.complete, model, messages, None if final else tools, max_tokens=8000
                    )
                    run.add_usage(response)
                    message = response["choices"][0]["message"]
                    calls = message.get("tool_calls") or []
                    messages.append({k: v for k, v in message.items() if k in ("role", "content", "tool_calls")})
                    if not calls:
                        answer = agent.parse_json(message.get("content") or "").get("answer")
                        break
                    messages.extend(await asyncio.gather(*(agent._call(client, run, tc) for tc in calls)))
        except Exception as exc:  # a failed question scores zero, it does not stop the eval
            error = str(exc)[:300]
        passed = grade(question["kind"], answer, gold, question.get("tolerance", 0.0))
        return {
            "id": question["id"],
            "question": question["q"],
            "answer": answer,
            "gold": gold,
            "passed": passed,
            "error": error,
            "tool_calls": [t.get("tool") for t in run.trace],
            "cost_usd": round(run.cost_usd, 5),
            "seconds": round(time.monotonic() - started, 1),
        }


async def qa(model: str) -> dict[str, Any]:
    spec = json.loads((HERE / "qa_questions.json").read_text())
    client = bq()
    golds = {q["id"]: list(client.query(q["sql"]).result())[0][0] for q in spec["questions"]}
    golds = {k: (float(v) if isinstance(v, (int, float)) else v) for k, v in golds.items()}
    semaphore = asyncio.Semaphore(CONCURRENCY)
    started = time.monotonic()
    results = await asyncio.gather(*(ask(model, q, golds[q["id"]], semaphore) for q in spec["questions"]))
    passed = sum(r["passed"] for r in results)
    return {
        "kind": "qa",
        "model": model,
        "score": round(passed / len(results), 3),
        "passed": passed,
        "total": len(results),
        "cost_usd": round(sum(r["cost_usd"] for r in results), 4),
        "seconds": round(time.monotonic() - started, 1),
        "details": results,
    }


def recall(edition_id: str | None) -> dict[str, Any]:
    spec = json.loads((HERE / "golden_week.json").read_text())
    client = bq()
    where = "edition_id = @id" if edition_id else "week_end = @week and purpose = 'eval'"
    row = list(
        client.query(
            f"select edition_id, model, to_json_string(edition) e, cost_usd from `{PROJECT}.ai.notable_editions` "
            f"where {where} order by generated_at desc limit 1",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("id", "STRING", edition_id or ""),
                    bigquery.ScalarQueryParameter("week", "DATE", spec["week_ending"]),
                ]
            ),
        ).result()
    )[0]
    regs = {
        r.registration.upper(): r.address
        for r in client.query(
            f"select registration, address from `{PROJECT}.public.pub_aircraft` where registration is not null"
        ).result()
    }
    blob = row.e.upper()
    details = []
    for item in spec["items"]:
        keys = [m.upper() for m in item["match"]]
        found = any(k in blob or (k in regs and regs[k] in blob) for k in keys)
        details.append({"item": item["item"], "found": found})
    passed = sum(d["found"] for d in details)
    return {
        "kind": "edition_recall",
        "model": row.model,
        "score": round(passed / len(details), 3),
        "passed": passed,
        "total": len(details),
        "cost_usd": row.cost_usd,
        "seconds": None,
        "details": {"edition_id": row.edition_id, "items": details},
    }


def save(result: dict[str, Any]) -> None:
    row = {"run_id": str(uuid.uuid4()), "started_at": datetime.now(UTC).isoformat(), **result}
    config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=[bigquery.SchemaField(n, t) for n, t in RUN_SCHEMA],
        write_disposition="WRITE_APPEND",
    )
    with tempfile.TemporaryFile() as f:
        f.write(json.dumps({n: row.get(n) for n, _ in RUN_SCHEMA}, default=str).encode() + b"\n")
        f.seek(0)
        bq().load_table_from_file(f, f"{PROJECT}.ai.eval_runs", job_config=config).result()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=["qa", "recall"])
    parser.add_argument("--model", action="append")
    parser.add_argument("--edition-id")
    args = parser.parse_args(argv[1:])
    results = (
        [asyncio.run(qa(m)) for m in (args.model or ["xiaomi/mimo-v2.6-pro"])]
        if args.kind == "qa"
        else [recall(args.edition_id)]
    )
    for result in results:
        save(result)
        summary = {k: result[k] for k in ("kind", "model", "score", "passed", "total", "cost_usd", "seconds")}
        print(json.dumps(summary))
        if result["kind"] == "qa":
            for d in result["details"]:
                mark = "PASS" if d["passed"] else "FAIL"
                print(f"  {mark} {d['id']:22s} answer={d['answer']!r} gold={d['gold']!r} {d['error'] or ''}")
        else:
            for d in result["details"]["items"]:
                print(f"  {'found  ' if d['found'] else 'MISSING'} {d['item']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
