"""Expose an MCP server's tools to an OpenAI-style tool-calling model."""

from __future__ import annotations

import json
from typing import Any

from mcp import Client

# Tools the agent may not use: run_sql needs a token the agent does not hold.
EXCLUDED = {"run_sql"}


def openai_tools(mcp_tools) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {
                "name": t.name, "description": t.description or "",
                "parameters": t.input_schema or {"type": "object", "properties": {}}}}
            for t in mcp_tools if t.name not in EXCLUDED]


def unwrap(result) -> Any:
    """A tool result as plain JSON: the structured content, without the
    {"result": ...} wrapper used for list results."""
    if result.structured_content is not None:
        content = result.structured_content
        if isinstance(content, dict) and set(content) == {"result"}:
            return content["result"]
        return content
    texts = [c.text for c in result.content if getattr(c, "text", None)]
    try:
        return json.loads(texts[0]) if len(texts) == 1 else texts
    except json.JSONDecodeError:
        return texts


async def call(client: Client, name: str, arguments: dict[str, Any]) -> tuple[bool, Any]:
    if name in EXCLUDED:
        return False, f"Tool {name} is not available to this agent."
    result = await client.call_tool(name, arguments)
    return not result.is_error, unwrap(result)
