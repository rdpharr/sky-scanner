"""Check every statement in a draft edition against the tool calls it cites.

A statement survives only if it cites at least one tool call, every cited
value matches what that call actually returned at the cited path, and every
cited number or name appears in the statement's text. Failing statements are
removed; entries and sections left with nothing to say are dropped. The
report lists every removal and why.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")
_PATH = re.compile(r"\[(\d+)\]|\.?([A-Za-z_][A-Za-z0-9_]*)")


class Missing(Exception):
    pass


def resolve(data: Any, path: str) -> Any:
    """Value at a path like 'rows[3].time_pt' or '[0].registration'."""
    position, value = 0, data
    for match in _PATH.finditer(path):
        if match.start() != position:
            raise Missing(path)
        position = match.end()
        index, key = match.groups()
        try:
            value = value[int(index)] if index is not None else value[key]
        except (KeyError, IndexError, TypeError):
            raise Missing(path) from None
    if position != len(path) or not path:
        raise Missing(path)
    return value


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(-?[\d,]*\.?\d+)\s*[a-zA-Z%°]*\s*", value)
        if match:
            return float(match.group(1).replace(",", ""))
    return None


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, str) and re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", value):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _renderings(moment: datetime) -> set[str]:
    """Ways a writer may render a UTC timestamp, in UTC or Pacific."""
    out = {moment.isoformat()}
    for t in (moment, moment.astimezone(PACIFIC)):
        hour12 = t.strftime("%I").lstrip("0") or "12"
        out |= {t.strftime("%Y-%m-%d"), t.strftime("%H:%M"), f"{hour12}:{t.strftime('%M')}",
                f"{hour12}:{t.strftime('%M')} {t.strftime('%p').lower()}"}
    return out


def matches(claimed: Any, actual: Any) -> bool:
    if isinstance(actual, list):
        return claimed == actual or any(matches(claimed, item) for item in actual)
    if isinstance(actual, bool) or isinstance(claimed, bool):
        return claimed == actual
    a, c = _number(actual), _number(claimed)
    if a is not None and c is not None:
        # Rounding only (14000.0 written as 14,000; 3.27 as 3.3): a count
        # rounded to "about 3,000" is not the 2,985 the data says.
        return abs(a - c) <= 0.5
    moment = _datetime(actual)
    if moment is not None and isinstance(claimed, str):
        return claimed.strip().lower() in {r.lower() for r in _renderings(moment)} or claimed == actual
    if isinstance(actual, str) and isinstance(claimed, str):
        a, c = actual.strip().casefold(), claimed.strip().casefold()
        return a == c or (len(c) >= 4 and c in a)
    return claimed == actual


_WORDS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen "
          "fourteen fifteen sixteen seventeen eighteen nineteen twenty").split()
_TENS = {30: "thirty", 40: "forty", 50: "fifty", 60: "sixty", 70: "seventy", 80: "eighty", 90: "ninety"}
_SPOKEN = {0: ("no", "none", "zero", "never"), 1: ("once", "a single", "one"),
           2: ("twice", "two", "both", "pair")}
_STOP = {"the", "a", "an", "of", "here", "heard", "this", "at", "in", "on", "and", "type", "for"}


def _number_variants(number: float) -> set[str]:
    out = {f"{number:.0f}", f"{number:g}", f"{number:.1f}", f"{number:.2f}", repr(number)}
    if number.is_integer():
        out.add(str(int(number)))
    return out


def in_text(claimed: Any, text: str) -> bool:
    """Is the cited value stated in the sentence? Numbers may be written as
    digits, rounded, or as words ("seven", "once", "no position"); a longer
    cited phrase may be paraphrased if most of its words appear. Dates and
    times may be written many ways, so they are not checked here."""
    plain = text.replace(",", "").casefold()
    number = _number(claimed)
    if number is not None:
        if any(re.search(rf"(?<![\d.]){re.escape(v)}(?![\d])", plain) for v in _number_variants(number)):
            return True
        n = int(number) if number.is_integer() else None
        words = list(_SPOKEN.get(n, ()))
        if n is not None and n < len(_WORDS):
            words.append(_WORDS[n])
        if n in _TENS:
            words.append(_TENS[n])
        return any(re.search(rf"\b{re.escape(w.strip())}\b", plain) for w in words)
    if isinstance(claimed, str) and not _datetime(claimed) and not re.fullmatch(r"[\d:\-T .apm]+", claimed):
        phrase = claimed.strip().casefold().replace(",", "")
        if phrase in plain:
            return True
        tokens = [t for t in re.findall(r"[a-z0-9\-/]+", phrase) if t not in _STOP]
        if len(tokens) >= 3:
            return sum(t in plain for t in tokens) / len(tokens) >= 0.6
        return False
    return True


def check_statement(statement: Any, calls: dict[str, Any]) -> tuple[bool, str]:
    if not isinstance(statement, dict) or not statement.get("text"):
        return False, "not a statement"
    evidence = statement.get("evidence") or []
    if not evidence:
        return False, "no evidence"
    for item in evidence:
        call = calls.get(item.get("call"))
        if call is None:
            return False, f"unknown call {item.get('call')!r}"
        try:
            actual = resolve(call, str(item.get("path", "")))
        except Missing:
            return False, f"no value at {item.get('call')}:{item.get('path')}"
        if not matches(item.get("value"), actual):
            return False, f"{item.get('call')}:{item.get('path')} is {actual!r}, not {item.get('value')!r}"
        if not in_text(item.get("value"), statement["text"]):
            return False, f"cited value {item.get('value')!r} is not in the sentence"
    return True, "verified"


def type_names(calls: dict[str, Any]) -> dict[str, set[str]]:
    """Every type_name the tools returned for each address."""
    names: dict[str, set[str]] = {}

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("address"), str) and isinstance(value.get("type_name"), str):
                names.setdefault(value["address"].upper(), set()).add(value["type_name"])
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(list(calls.values()))
    return names


def checked_title(entry: dict[str, Any], names: dict[str, set[str]]) -> tuple[str, str | None]:
    """An entry's title must contain the type name the data gives for one of
    its aircraft; otherwise it is replaced by that name. Returns (title,
    correction or None)."""
    title = entry.get("title") or ""
    found = sorted({n for a in entry.get("addresses", []) for n in names.get(str(a).upper(), set())})
    if not found or any(n.casefold() in title.casefold() for n in found):
        return title, None
    return found[0], f"title {title!r} replaced by the data's type name {found[0]!r}"


def verify(draft: dict[str, Any], calls: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (edition with only verified statements, report)."""
    report = {"checked": 0, "verified": 0, "removed": [], "titles_corrected": []}
    names = type_names(calls)

    def keep(statements: Any, where: str) -> list[dict]:
        kept = []
        for s in statements if isinstance(statements, list) else [statements]:
            if s is None:
                continue
            report["checked"] += 1
            ok, reason = check_statement(s, calls)
            if ok:
                report["verified"] += 1
                kept.append({"text": s["text"], "evidence": s["evidence"]})
            else:
                report["removed"].append({"where": where, "text": (s or {}).get("text") if isinstance(s, dict) else str(s),
                                          "reason": reason})
        return kept

    edition = {"title": draft.get("title"), "subtitle": draft.get("subtitle"),
               "summary": keep(draft.get("summary", []), "summary"), "sections": []}
    for section in draft.get("sections", []):
        heading = section.get("heading", "")
        entries = []
        for entry in section.get("entries", []):
            where = f"{heading} / {entry.get('title')}"
            details = keep(entry.get("details", []), where)
            what = keep(entry.get("what_it_is"), where + " (what it is)")
            if details:
                title, correction = checked_title(entry, names)
                if correction:
                    report["titles_corrected"].append(correction)
                entries.append({"title": title, "addresses": entry.get("addresses", []),
                                "designator": entry.get("designator"),
                                "what_it_is": what[0] if what else None, "details": details})
            else:
                report["removed"].append({"where": where, "text": None, "reason": "entry dropped: no verified details"})
        intro = keep(section.get("intro", []), heading)
        if entries or intro:
            edition["sections"].append({"heading": heading, "intro": intro, "entries": entries})
    edition["privacy"] = keep(draft.get("privacy", []), "privacy")
    edition["caveats"] = keep(draft.get("caveats", []), "caveats")
    report["verification_rate"] = round(report["verified"] / report["checked"], 3) if report["checked"] else 0
    return edition, report
