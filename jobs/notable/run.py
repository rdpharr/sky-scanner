"""Write, verify and store a weekly notable-aircraft edition.

Usage: run.py edition [--week-ending YYYY-MM-DD] [--model MODEL] [--purpose weekly|eval|test]

week-ending is the Pacific date after the week's last day (default today in
Pacific time, so a Monday run covers Monday to Sunday). Editions with
purpose "weekly" are published; eval and test editions are stored for
comparison only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from google.cloud import bigquery

import agent
import publish
import verifier

MCP_URL = os.environ.get("MCP_URL", "https://mcp.skyscanner.pharrhomelab.com/mcp")
MODEL = os.environ.get("NOTABLE_MODEL", "xiaomi/mimo-v2.6-pro")
PROJECT = os.environ.get("GCP_PROJECT", "sky-scanner-pharr")
AI_DATASET = os.environ.get("AI_DATASET", "ai")
PUBLIC_DATASET = os.environ.get("PUBLIC_DATASET", "public")
# An edition is published only if this share of its statements verified.
MIN_VERIFICATION_RATE = float(os.environ.get("MIN_VERIFICATION_RATE", 0.6))


def log(severity: str, message: str, **fields) -> None:
    print(json.dumps({"severity": severity, "message": message, **fields}, default=str), flush=True)


async def edition(week_ending: date, model: str, purpose: str) -> dict:
    start = week_ending - timedelta(days=7)
    log("INFO", f"writing edition {start} to {week_ending} with {model}", event="edition_started")
    draft, run = await agent.write_edition(MCP_URL, model, start, week_ending)
    verified, report = verifier.verify(draft, run.calls)
    entries = sum(len(s["entries"]) for s in verified["sections"])
    publishable = report["verification_rate"] >= MIN_VERIFICATION_RATE and entries >= 3
    status = ("published" if publishable else "rejected") if purpose == "weekly" else purpose
    client = bigquery.Client(project=PROJECT)
    designators = [e["designator"] for s in verified["sections"] for e in s["entries"] if e.get("designator")]
    row = {
        "edition_id": str(uuid.uuid4()), "week_start": start.isoformat(), "week_end": week_ending.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(), "purpose": purpose, "model": model,
        "status": status, "edition": verified, "draft": draft, "verification": report,
        "photos": publish.photos(client, PUBLIC_DATASET, designators), "trace": run.trace,
        "tool_calls": len(run.trace), "turns": run.turns, "prompt_tokens": run.prompt_tokens,
        "completion_tokens": run.completion_tokens, "cost_usd": round(run.cost_usd, 4),
        "seconds": round(run.seconds, 1), "final_text": run.final_text, "finish_reason": run.finish_reason,
        "outline": run.outline,
    }
    publish.save(client, AI_DATASET, row)
    summary = {k: row[k] for k in ("edition_id", "status", "tool_calls", "turns", "cost_usd", "seconds")}
    summary.update(statements=report["checked"], verified=report["verified"],
                   verification_rate=report["verification_rate"], entries=entries)
    severity = "ERROR" if status == "rejected" else "INFO"
    log(severity, f"edition {status}: {report['verified']}/{report['checked']} statements verified, "
                  f"{entries} entries, ${row['cost_usd']}", event=f"edition_{status}", **summary)
    return summary


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["edition"])
    parser.add_argument("--week-ending", default="")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--purpose", default="weekly", choices=["weekly", "eval", "test"])
    args = parser.parse_args(argv[1:])
    week_ending = (date.fromisoformat(args.week_ending) if args.week_ending
                   else datetime.now(ZoneInfo("America/Los_Angeles")).date())
    summary = asyncio.run(edition(week_ending, args.model, args.purpose))
    return 0 if summary["status"] != "rejected" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
