"""Tool-calling agent: an OpenRouter model investigating through the MCP server.

Two steps. Investigate: the model calls tools (each result labeled with a call
id) and ends with a small outline saying which calls support each part of
the report. Write: each entry, section intro and the front matter is
written in parallel from only the tool results it needs, citing call ids as
evidence. Small parts keep every model output short, so nothing is cut off
at the output limit, and a failed part loses one entry, not a section. The trace keeps each call's arguments and result for
the verifier and for publication.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from mcp import Client

import mcp_bridge
import openrouter

PROMPTS = Path(__file__).parent / "prompts"
MAX_TURNS = 30
MAX_TOOL_CALLS = 50
MAX_RESULT_CHARS = 12_000
SYSTEM = ("You are a careful data journalist. You use tools to investigate and you cite every fact. "
          "You never state anything the tools did not return.")


@dataclass
class Run:
    model: str
    trace: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    turns: int = 0
    seconds: float = 0.0
    outline: dict[str, Any] = field(default_factory=dict)
    final_text: str = ""
    finish_reason: str = ""

    @property
    def calls(self) -> dict[str, Any]:
        return {t["call_id"]: t["result"] for t in self.trace if t.get("ok")}

    def add_usage(self, response: dict[str, Any]) -> None:
        usage = response.get("usage") or {}
        self.prompt_tokens += usage.get("prompt_tokens") or 0
        self.completion_tokens += usage.get("completion_tokens") or 0
        self.cost_usd += usage.get("cost") or 0.0


def prompt(name: str, start: date, end: date) -> str:
    # Plain replacement: the prompts contain literal JSON braces.
    text = (PROMPTS / f"{name}.md").read_text()
    for key, value in {"start_date": start, "end_date": end, "last_date": end - timedelta(days=1)}.items():
        text = text.replace("{" + key + "}", value.isoformat())
    return text


def parse_json(text: str) -> dict[str, Any]:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


def json_reply(run: Run, messages: list[dict], required: str, attempts: int = 2) -> dict[str, Any]:
    """Ask for JSON with a required key; on a bad reply, ask once more."""
    finish = None
    for _ in range(attempts):
        response = openrouter.complete(run.model, messages, max_tokens=16000)
        run.add_usage(response)
        finish = response["choices"][0].get("finish_reason")
        text = response["choices"][0]["message"].get("content") or ""
        try:
            data = parse_json(text)
            if required in data:
                return data
        except (ValueError, json.JSONDecodeError):
            pass
        messages = messages + [{"role": "assistant", "content": text[:20000]},
                               {"role": "user", "content": f"That was not valid JSON with a \"{required}\" "
                                                           "key. Reply with only the JSON."}]
    raise RuntimeError(f"model gave no valid JSON with {required!r} (finish_reason={finish})")


async def _call(client: Client, run: Run, tc: dict[str, Any]) -> dict[str, Any]:
    call_id = f"c{len(run.trace) + 1}"
    entry: dict[str, Any] = {"call_id": call_id}
    run.trace.append(entry)  # reserve the id in request order
    name, arguments = tc["function"]["name"], {}
    t0 = time.monotonic()
    try:
        arguments = json.loads(tc["function"].get("arguments") or "{}")
        ok, result = await mcp_bridge.call(client, name, arguments)
    except Exception as exc:  # a bad call is reported to the model, not fatal
        ok, result = False, str(exc)
    entry.update(tool=name, arguments=arguments, ok=ok, result=result,
                 seconds=round(time.monotonic() - t0, 2))
    content = json.dumps({"call_id": call_id, "ok": ok, "result": result}, default=str)
    if len(content) > MAX_RESULT_CHARS:
        content = content[:MAX_RESULT_CHARS] + "... [truncated: narrow the request]"
    return {"role": "tool", "tool_call_id": tc["id"], "content": content}


async def investigate(client: Client, run: Run, task: str) -> dict[str, Any]:
    """Let the model call tools; returns its outline."""
    tools = mcp_bridge.openai_tools((await client.list_tools()).tools)
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM},
                                      {"role": "user", "content": task}]
    while True:
        run.turns += 1
        if len(run.trace) >= MAX_TOOL_CALLS or run.turns >= MAX_TURNS:
            messages.append({"role": "user", "content": "Stop investigating. Reply with the outline JSON now, "
                                                        "citing only calls you have made."})
            return json_reply(run, messages, "sections")
        response = openrouter.complete(run.model, messages, tools, max_tokens=16000)
        run.add_usage(response)
        message = response["choices"][0]["message"]
        tool_calls = message.get("tool_calls") or []
        messages.append({k: v for k, v in message.items() if k in ("role", "content", "tool_calls")})
        if not tool_calls:
            try:
                outline = parse_json(message.get("content") or "")
                if "sections" in outline:
                    return outline
            except (ValueError, json.JSONDecodeError):
                pass
            messages.append({"role": "user", "content": "Reply with only the outline JSON."})
            return json_reply(run, messages, "sections")
        # The model may request several calls at once: run them together.
        messages.extend(await asyncio.gather(*(_call(client, run, tc) for tc in tool_calls)))


def _results(run: Run, call_ids: list[str]) -> str:
    calls = run.calls
    chosen = {cid: calls[cid] for cid in dict.fromkeys(call_ids) if cid in calls}
    text = json.dumps(chosen, default=str)
    return text if len(text) < 60_000 else text[:60_000] + "... [truncated]"


def _write(run: Run, system: str, request: str, call_ids: list[str], required: str) -> dict[str, Any]:
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": f"{request}\n\nTool results by call_id:\n{_results(run, call_ids)}"}]
    return json_reply(run, messages, required)


async def write_edition(mcp_url: str, model: str, start: date, end: date) -> tuple[dict[str, Any], Run]:
    run = Run(model=model)
    started = time.monotonic()
    async with Client(mcp_url) as client:
        run.outline = await investigate(client, run, prompt("investigate", start, end))
    outline = run.outline
    system = prompt("write", start, end)

    limit = asyncio.Semaphore(6)  # parallel requests to the provider

    async def bounded(fn, *args):
        async with limit:
            return await asyncio.to_thread(fn, *args)

    def entry(e: dict[str, Any]) -> dict[str, Any]:
        request = ("Write this entry. Reply with {\"title\": ..., \"addresses\": [...], \"designator\": ..., "
                   "\"what_it_is\": statement, \"details\": [statement, ...]} (1 to 4 details).\n\nEntry outline:\n"
                   + json.dumps(e))
        return _write(run, system, request, list(e.get("calls", [])), "details")

    def intro(s: dict[str, Any]) -> dict[str, Any]:
        request = ("Write this section's heading and a short introduction (1 to 3 statements). Reply with "
                   "{\"heading\": ..., \"intro\": [statement, ...]}.\n\nSection outline:\n"
                   + json.dumps({k: v for k, v in s.items() if k != "entries"}))
        return _write(run, system, request, list(s.get("intro_calls", [])), "intro")

    def front_matter() -> dict[str, Any]:
        calls = (list(outline.get("summary_calls", [])) + list(outline.get("privacy_calls", []))
                 + list(outline.get("caveats_calls", [])))
        request = ("Write the report's title, subtitle, summary (2 to 4 statements: lead with the week's most "
                   "distinctive story, then totals), privacy (LADD and PIA counts) and caveats. Reply with "
                   "{\"title\": ..., \"subtitle\": ..., \"summary\": [statement, ...], \"privacy\": [...], "
                   "\"caveats\": [...]}. Section headings, for context: "
                   + json.dumps([s.get("heading") for s in outline.get("sections", [])])
                   + "\nCaveat notes from the investigation: " + json.dumps(outline.get("caveats", [])))
        return _write(run, system, request, calls, "summary")

    # One request per entry and per section intro: every output stays small,
    # and a failure loses one entry, not a section.
    sections = outline.get("sections", [])
    front, intros, entries = await asyncio.gather(
        bounded(front_matter),
        asyncio.gather(*(bounded(intro, s) for s in sections), return_exceptions=True),
        asyncio.gather(*(asyncio.gather(*(bounded(entry, e) for e in s.get("entries", [])),
                                        return_exceptions=True) for s in sections)),
        return_exceptions=True)
    if isinstance(front, Exception):
        raise front
    failed = []
    draft_sections = []
    for s, head, written in zip(sections, intros, entries):
        if isinstance(head, Exception):
            failed.append(f"{s.get('heading')}: intro: {head}")
            head = {"heading": s.get("heading"), "intro": []}
        kept = []
        for e, result in zip(s.get("entries", []), written):
            if isinstance(result, Exception):
                failed.append(f"{s.get('heading')} / {e.get('title')}: {result}")
            else:
                kept.append(result)
        draft_sections.append({"heading": head.get("heading") or s.get("heading"),
                               "intro": head.get("intro", []), "entries": kept})
    draft = {**front, "sections": draft_sections}
    run.final_text = json.dumps({"failed_parts": failed}) if failed else ""
    run.seconds = time.monotonic() - started
    return draft, run
