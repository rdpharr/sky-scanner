"""tar1090-db aircraft database (hex -> registration, type, description).

wiedehopf/tar1090-db republishes the Mictronics aircraft database daily as a
semicolon-separated ``aircraft.csv.gz``:
``hex;registration;icao_type;flags;description;year;owner_operator;``.
``flags`` is a bit string: military, interesting, privacy ICAO address (PIA),
LADD. The repository states no license, so the data is used only in this
private database and never committed. Only aircraft actually observed are
stored, keeping storage proportional to what the receiver hears.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import re
import urllib.request
from datetime import UTC, datetime
from typing import Any

SOURCE_NAME = "tar1090_db"
URL = "https://raw.githubusercontent.com/wiedehopf/tar1090-db/csv/aircraft.csv.gz"
USER_AGENT = "SkyScanner/1.0 (personal receive-only ADS-B station; daily download)"
MAX_BYTES = 80 * 1024 * 1024
FLAG_NAMES = ("military", "interesting", "privacy ICAO address (PIA)", "LADD")


def download() -> bytes:
    request = urllib.request.Request(URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError(f"tar1090-db archive exceeds {MAX_BYTES} bytes")
    if not data:
        raise ValueError("tar1090-db download was empty")
    return data


def _clean(value: str | None) -> str | None:
    text = re.sub(r"\s+", " ", value or "").strip()
    return text or None


def flag_names(flags: str | None) -> list[str]:
    return [name for bit, name in zip(flags or "", FLAG_NAMES) if bit == "1"]


def parse(data: bytes) -> tuple[str, dict[str, tuple]]:
    """Return (sha256 of the archive, hex -> (reg, type, flags, desc, year, owner_operator))."""
    digest = hashlib.sha256(data).hexdigest()
    text = gzip.decompress(data).decode("utf-8", errors="replace")
    table: dict[str, tuple] = {}
    for row in csv.reader(io.StringIO(text), delimiter=";"):
        if len(row) < 7:
            continue
        address = row[0].strip().upper()
        if re.fullmatch(r"[0-9A-F]{6}", address) and address not in table:
            table[address] = tuple(_clean(v) for v in row[1:7])
    if len(table) < 100_000:
        raise ValueError(f"tar1090-db has only {len(table)} aircraft")
    return digest, table


def records(
    table: dict[str, tuple], addresses, *, dataset_hash: str, retrieved_at: datetime | None = None
) -> list[dict[str, Any]]:
    """Metadata records for the observed ``addresses`` found in the table."""
    retrieved = (retrieved_at or datetime.now(UTC)).astimezone(UTC)
    result = []
    for address in sorted({a.upper() for a in addresses}):
        entry = table.get(address)
        if not entry:
            continue
        registration, icao_type, flags, description, _year, owner_operator = entry
        # Miscode rows mark invalid transponder addresses; the text can sit in
        # any column and their flags are meaningless.
        miscode = any((v or "").lower().startswith("miscode") for v in entry)
        tags = ["miscode"] if miscode else flag_names(flags)
        result.append(
            {
                "address_type": "icao",
                "icao_address": address,
                "registration": registration,
                "icao_type_code": icao_type,
                "model": description,
                "registered_owner": owner_operator,
                "tags": ", ".join(tags) or None,
                "source_url": URL,
                "dataset_hash": dataset_hash,
                "retrieved_at": retrieved.isoformat().replace("+00:00", "Z"),
            }
        )
    return result
