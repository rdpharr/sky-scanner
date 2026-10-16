"""Minimal OpenRouter chat-completions client (OpenAI-compatible, with tools)."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

URL = "https://openrouter.ai/api/v1/chat/completions"


def complete(model: str, messages: list[dict], tools: list[dict] | None = None, *,
             max_tokens: int = 32000, temperature: float = 0.2, retries: int = 3,
             reasoning_effort: str | None = "low") -> dict[str, Any]:
    body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens,
                            "temperature": temperature, "usage": {"include": True}}
    if reasoning_effort:
        # Reasoning models think before answering; "low" cuts time and tokens.
        body["reasoning"] = {"effort": reasoning_effort}
    if tools:
        body["tools"] = tools
    request = urllib.request.Request(
        URL, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
                 "Content-Type": "application/json",
                 "HTTP-Referer": "https://skyscanner.pharrhomelab.com", "X-Title": "Sky Scanner"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                data = json.load(response)
            if "choices" not in data:
                raise RuntimeError(f"OpenRouter error: {str(data)[:300]}")
            return data
        except (urllib.error.URLError, TimeoutError, RuntimeError):
            if attempt == retries - 1:
                raise
            time.sleep(5 * (attempt + 1))
    raise AssertionError("unreachable")
