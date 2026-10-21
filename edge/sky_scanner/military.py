"""Military aircraft identification and decoding (military aircraft only).

Detection uses local reference data checked against every observed aircraft
(no per-aircraft network calls):

* the US military ICAO address block (AE0000-AFFFFF);
* the plane-alert-db military list (ODbL), keyed by ICAO hex address;
* tar1090-db's military flag;
* military ICAO operator codes at the start of a callsign (RCH123 -> Air
  Mobility Command), from Virtual Radar Server standing data (CC0).

These are the rules of dbt's ``int_military_classification``, which is the
published definition; the edge uses its copy only to choose what to look up.
Only aircraft flagged by one of those checks have their tactical callword
(TALON95 -> TALON) looked up in the War Watch Intel callsign codex. Lookups are
cached, rate limited, and describe which aircraft types and countries have
recently used a callword; they never identify a specific unit.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import re
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

PLANE_ALERT_SOURCE = "plane_alert_mil"
PLANE_ALERT_MIL_URL = "https://raw.githubusercontent.com/sdr-enthusiasts/plane-alert-db/main/plane-alert-mil.csv"
OPERATOR_CODES_URL = "https://raw.githubusercontent.com/vradarserver/standing-data/main/airlines/schema-01/airlines.csv"
WWI_FAMILY_URL = "https://www.warwatchintel.com/codex/callsign_family/{word}"
USER_AGENT = "SkyScanner/1.0 (personal receive-only ADS-B station; cached lookups)"
MAX_REFERENCE_BYTES = 20 * 1024 * 1024

MILITARY_OPERATOR = re.compile(
    r"air force|navy|naval|army|marine|military|coast guard|armed forces|defen[cs]e force|"
    r"canadian forces|air mobility|special air mission|priority air transport|luftwaffe|"
    r"armee|aeronautica militare|fuerza a[eé]rea",
    re.I,
)
US_MILITARY_BLOCK = ("AE", "AF")
CALLWORD = re.compile(r"^([A-Z]{3,})(\d{1,4}[A-Z]?)$")


def _download(url: str, timeout: int = 60) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_REFERENCE_BYTES + 1)
    if len(data) > MAX_REFERENCE_BYTES:
        raise ValueError(f"{url} exceeds {MAX_REFERENCE_BYTES} bytes")
    if not data:
        raise ValueError(f"{url} returned no data")
    return data


def download_plane_alert_mil() -> bytes:
    return _download(PLANE_ALERT_MIL_URL)


def download_operator_codes() -> bytes:
    return _download(OPERATOR_CODES_URL)


def _clean(value: Any) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    # plane-alert-db uses runs of "?" as an "unknown" placeholder.
    return text if text and not re.fullmatch(r"\?+", text) else None


def parse_plane_alert_mil(
    data: bytes, *, source_url: str = PLANE_ALERT_MIL_URL, retrieved_at: datetime | None = None
) -> list[dict[str, Any]]:
    """Parse plane-alert-mil.csv into metadata records (one per ICAO address)."""
    retrieved = (retrieved_at or datetime.now(UTC)).astimezone(UTC)
    dataset_hash = hashlib.sha256(data).hexdigest()
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig", errors="replace")))
    # Headers carry display prefixes such as "$ICAO" and "$#Tag 2".
    reader.fieldnames = [name.lstrip("$#").strip() for name in (reader.fieldnames or [])]
    if "ICAO" not in reader.fieldnames:
        raise ValueError("plane-alert-db military list is missing the ICAO column")
    records, seen = [], set()
    for row in reader:
        address = (row.get("ICAO") or "").strip().upper()
        if not re.fullmatch(r"[0-9A-F]{6}", address) or address in seen:
            continue
        seen.add(address)
        tags = [t for t in (_clean(row.get(k)) for k in ("Tag 1", "Tag 2", "Tag 3")) if t]
        records.append(
            {
                "address_type": "icao",
                "icao_address": address,
                "registration": _clean(row.get("Registration")),
                "operator": _clean(row.get("Operator")),
                "model": _clean(row.get("Type")),
                "icao_type_code": _clean(row.get("ICAO Type")),
                "category": _clean(row.get("Category")),
                "tags": ", ".join(tags) or None,
                "source_url": source_url,
                "retrieved_at": retrieved.isoformat().replace("+00:00", "Z"),
                "dataset_hash": dataset_hash,
            }
        )
    return records


def parse_military_operators(data: bytes) -> list[dict[str, str]]:
    """Military operators (ICAO designator -> name) from VRS airlines.csv."""
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig", errors="replace")))
    if not {"ICAO", "Name"} <= set(reader.fieldnames or []):
        raise ValueError("operator code file is missing ICAO/Name columns")
    result = {}
    for row in reader:
        code, name = (row.get("ICAO") or "").strip().upper(), _clean(row.get("Name"))
        if re.fullmatch(r"[A-Z]{3}", code) and name and MILITARY_OPERATOR.search(name):
            result.setdefault(code, {"code": code, "name": name})
    return list(result.values())


def military_evidence(
    address: str, plane_alert: dict | None, operator_code_name: str | None, tar1090: dict | None = None
) -> list[str]:
    """Why an aircraft counts as military (empty list: not military)."""
    evidence = []
    if address.upper().startswith(US_MILITARY_BLOCK):
        evidence.append("US military ICAO address block")
    if plane_alert:
        evidence.append("plane-alert-db military list")
    if tar1090 and "military" in (tar1090.get("tags") or "").split(", "):
        evidence.append("tar1090-db military flag")
    if operator_code_name:
        evidence.append(f"military operator code ({operator_code_name})")
    return evidence


def callword(callsign: str | None, operator_codes: set[str] = frozenset()) -> str | None:
    """Tactical callword of a callsign (TALON95 -> TALON), or None.

    Operator-code callsigns (RCH123) and registrations are not callwords.
    """
    match = CALLWORD.match((callsign or "").strip().upper())
    if not match:
        return None
    word = match.group(1)
    if len(word) == 3 and word in operator_codes:
        return None
    return word


def _visible_lines(page: str) -> list[str]:
    start, end = page.find("<main"), page.find("</main>")
    body = page[start:end] if start >= 0 and end > start else page
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", body, flags=re.S | re.I)
    body = re.sub(r"<[^>]+>", "\n", body)
    return [line for line in (html.unescape(part).strip() for part in body.split("\n")) if line]


def parse_wwi_family(page: str, word: str) -> dict[str, Any] | None:
    """Parse a War Watch Intel callsign-family page; None when it is a 404 page.

    Every page embeds the site's not-found template, so a missing family is
    recognised by its title (and by HTTP 404 in ``fetch_wwi_family``).
    """
    title = re.search(r"<title>([^<]*)</title>", page)
    if title and title.group(1).startswith("404"):
        return None
    lines = _visible_lines(page)
    if word.upper() not in lines:
        raise ValueError(f"unexpected War Watch Intel page layout for {word}")

    def after(label: str) -> str | None:
        return lines[lines.index(label) + 1] if label in lines and lines.index(label) + 1 < len(lines) else None

    def pairs(start: str, stop: str) -> list[dict[str, Any]]:
        if start not in lines:
            return []
        items = lines[lines.index(start) + 1 :]
        items = items[: items.index(stop)] if stop in items else items
        out = []
        for name, count in zip(items[0::2], items[1::2]):
            if count.isdigit():
                out.append({"name": name, "count": int(count)})
        return out

    def integer(label: str) -> int | None:
        value = after(label)
        return int(value) if value and value.isdigit() else None

    summary = next((line for line in lines if line.startswith("Seen on ")), None)
    examples = after("Examples")
    return {
        "word": word.upper(),
        "summary": summary,
        "days": integer("Days"),
        "airframes": integer("Airframes"),
        "types": pairs("Types", "Countries"),
        "countries": pairs("Countries", "Examples"),
        "examples": [e.strip() for e in examples.split(",")] if examples else [],
        "first_seen": after("first seen"),
        "last_seen": after("last seen"),
    }


def fetch_wwi_family(word: str, timeout: int = 30) -> tuple[str, dict[str, Any] | None]:
    """Return (url, parsed family or None when War Watch Intel has no page)."""
    url = WWI_FAMILY_URL.format(word=urllib.request.quote(word.upper()))
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            page = response.read(2 * 1024 * 1024).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return url, None
        raise
    return url, parse_wwi_family(page, word)
