"""Parse the FAA releasable aircraft registry into enrichment records.

The parser accepts the comma-delimited MASTER.txt and ACFTREF.txt files in a
ReleasableAircraft.zip archive. Column headers are preferred because FAA file
layouts can change. For headerless legacy extracts, the documented leading
fields and historical Mode S column are used as a fallback.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

FAA_REGISTRY_URL = (
    "https://www.faa.gov/licenses_certificates/aircraft_certification/aircraft_registry/releasable_aircraft_download"
)


def normalize_icao(value: Any) -> str | None:
    """Return a six-character uppercase ICAO hex address, preserving leading 0s."""
    text = str(value or "").strip().upper()
    text = re.sub(r"^(?:0X)", "", text)
    if not re.fullmatch(r"[0-9A-F]{1,6}", text):
        return None
    return text.zfill(6)


def normalize_registration(value: Any) -> str | None:
    """Normalize an FAA N-number to the conventional ``N123AB`` form."""
    text = re.sub(r"\s+", "", str(value or "").strip().upper())
    if not text or text in {"UNKNOWN", "ANONYMOUS", "WITHHELD", "NONE", "NULL"}:
        return None
    if not text.startswith("N"):
        text = "N" + text
    # FAA registrations have at most five characters after the N prefix.
    if len(text) > 6 or not re.fullmatch(r"N[1-9][0-9]{0,4}[A-HJ-NP-Z]{0,2}", text):
        return None
    return text


def _key(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.strip().upper().lstrip("\ufeff"))


def _header_map(row: list[str]) -> dict[str, int] | None:
    keys = [_key(cell) for cell in row]
    master_header = any(k in {"NNUMBER", "REGISTRATION", "MODESCODEHEX", "MODESCODE"} for k in keys)
    reference_header = any(
        k in {"CODE", "AIRCRAFTMANUFACTURERMODELCODE", "AIRCRAFTMANUFACTURERMODELANDSERIESCODE"} for k in keys
    ) and any(k in {"MFR", "MANUFACTURER", "MANUFACTURERNAME", "AIRCRAFTMANUFACTURERNAME"} for k in keys)
    if not (master_header or reference_header):
        return None
    return {key: i for i, key in enumerate(keys) if key}


def _has(headers: dict[str, int], *names: str) -> bool:
    return any(_key(name) in headers for name in names)


def _validate_headers(headers: dict[str, int] | None, kind: str) -> None:
    if headers is None:
        return
    if kind == "master":
        requirements = (
            ("N-NUMBER", "REGISTRATION"),
            ("MFR MDL CODE", "AIRCRAFT MFR MODEL CODE", "AIRCRAFT MANUFACTURER MODEL CODE"),
            ("MODE S CODE HEX", "MODE S HEX", "ICAO ADDRESS"),
        )
    else:
        requirements = (
            ("CODE", "MFR MDL CODE", "AIRCRAFT MANUFACTURER MODEL CODE", "AIRCRAFT MANUFACTURER MODEL AND SERIES CODE"),
            ("MFR", "MANUFACTURER", "MANUFACTURER NAME", "AIRCRAFT MANUFACTURER NAME"),
            ("MODEL", "MODEL NAME"),
            ("TYPE-ACFT", "TYPE AIRCRAFT", "AIRCRAFT TYPE"),
        )
    if any(not _has(headers, *names) for names in requirements):
        raise ValueError(f"malformed FAA {kind.upper()} header")


def _rows(data: bytes, kind: str) -> tuple[dict[str, int] | None, list[list[str]]]:
    text = data.decode("utf-8-sig", errors="replace")
    parsed = list(csv.reader(io.StringIO(text, newline="")))
    parsed = [row for row in parsed if any(cell.strip() for cell in row)]
    if not parsed:
        return None, []
    headers = _header_map(parsed[0])
    if headers is not None:
        _validate_headers(headers, kind)
        return headers, parsed[1:]
    keys = {_key(cell) for cell in parsed[0]}
    looks_like_header = (
        "SERIALNUMBER" in keys
        or "NNUMBER" in keys
        or "REGISTRATION" in keys
        or "TYPEACFT" in keys
        or "TCDATASHEET" in keys
    )
    if looks_like_header:
        raise ValueError(f"malformed FAA {kind.upper()} header")
    return None, parsed


def _col(row: list[str], headers: dict[str, int] | None, names: Iterable[str], fallback: int) -> str:
    if headers:
        for name in names:
            i = headers.get(_key(name))
            if i is not None and i < len(row):
                return row[i].strip()
    return row[fallback].strip() if fallback < len(row) else ""


def _optional(value: str) -> str | None:
    value = value.strip()
    if not value or value.upper() in {"UNKNOWN", "ANONYMOUS", "WITHHELD", "NONE", "NULL", "N/A"}:
        return None
    return value


def parse_faa_registry(
    archive: bytes | bytearray | io.BufferedIOBase,
    *,
    source_url: str = FAA_REGISTRY_URL,
    retrieved_at: datetime | str | None = None,
) -> list[dict[str, Any]]:
    """Parse an FAA registry ZIP into aircraft metadata dictionaries.

    Each result has ``icao_address``, ``registration``, ``manufacturer``,
    ``model``, ``aircraft_type``, ``registered_owner``, and a null ``operator``.
    Provenance fields are ``source_url``, UTC ISO ``retrieved_at``, and the
    SHA-256 ``dataset_hash`` of the supplied ZIP bytes. Aircraft without a
    valid ICAO hex address or FAA N-number are omitted.

    ``archive`` may be ZIP bytes or a binary file object. A ValueError is
    raised when required archive members are missing or the ZIP is invalid.
    """
    if hasattr(archive, "read"):
        archive = archive.read()
    payload = bytes(archive)
    try:
        zf = zipfile.ZipFile(io.BytesIO(payload))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError("invalid FAA registry ZIP archive") from exc
    members = {name.rsplit("/", 1)[-1].upper(): name for name in zf.namelist()}
    missing = [name for name in ("MASTER.TXT", "ACFTREF.TXT") if name not in members]
    if missing:
        raise ValueError("FAA registry ZIP missing required file(s): " + ", ".join(missing))

    if retrieved_at is None:
        retrieved = datetime.now(UTC)
    elif isinstance(retrieved_at, str):
        retrieved = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
    else:
        retrieved = retrieved_at
    if retrieved.tzinfo is None:
        retrieved = retrieved.replace(tzinfo=UTC)
    retrieved_text = retrieved.astimezone(UTC).isoformat().replace("+00:00", "Z")
    dataset_hash = hashlib.sha256(payload).hexdigest()

    reference_headers, reference_rows = _rows(zf.read(members["ACFTREF.TXT"]), "reference")
    references: dict[str, dict[str, str | None]] = {}
    for row in reference_rows:
        code = _col(
            row,
            reference_headers,
            (
                "CODE",
                "MFR MDL CODE",
                "AIRCRAFT MFR MODEL CODE",
                "AIRCRAFT MANUFACTURER MODEL CODE",
                "AIRCRAFT MANUFACTURER MODEL AND SERIES CODE",
            ),
            0,
        )
        if not code:
            continue
        references[code.strip()] = {
            "manufacturer": _optional(
                _col(
                    row,
                    reference_headers,
                    ("MFR", "MANUFACTURER", "MANUFACTURER NAME", "AIRCRAFT MANUFACTURER NAME"),
                    1,
                )
            ),
            "model": _optional(_col(row, reference_headers, ("MODEL", "MODEL NAME"), 2)),
            "aircraft_type": _optional(
                _col(row, reference_headers, ("TYPE-ACFT", "TYPE AIRCRAFT", "AIRCRAFT TYPE"), 3)
            ),
        }

    master_headers, master_rows = _rows(zf.read(members["MASTER.TXT"]), "master")
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in master_rows:
        registration = normalize_registration(_col(row, master_headers, ("N-NUMBER", "N NUMBER", "REGISTRATION"), 0))
        # FAA's current layout identifies Mode S Code Hex by header. The fallback
        # index supports the common headerless 34-column release format.
        hex_value = _col(row, master_headers, ("MODE S CODE HEX", "MODE S HEX", "ICAO ADDRESS"), 33)
        icao = normalize_icao(hex_value)
        if not registration or not icao or icao in seen:
            continue
        code = _col(
            row, master_headers, ("MFR MDL CODE", "AIRCRAFT MFR MODEL CODE", "AIRCRAFT MANUFACTURER MODEL CODE"), 2
        )
        owner = _optional(_col(row, master_headers, ("NAME", "REGISTRANT NAME", "REGISTRANT'S NAME", "OWNER"), 6))
        # FAA codes: 1 individual, 2 partnership, 3 corporation, 4 co-owned,
        # 5 government, 7 LLC, 8 non-citizen corporation, 9 non-citizen co-owned.
        registrant = _col(row, master_headers, ("TYPE REGISTRANT", "REGISTRANT TYPE"), 5)
        year = _col(row, master_headers, ("YEAR MFR", "YEAR MANUFACTURED"), 4)
        metadata = references.get(code, {})
        results.append(
            {
                "icao_address": icao,
                "registration": registration,
                "manufacturer": metadata.get("manufacturer"),
                "model": metadata.get("model"),
                "aircraft_type": metadata.get("aircraft_type"),
                "registered_owner": owner,
                "registrant_type": registrant if re.fullmatch(r"[1-9]", registrant) else None,
                "year_mfr": int(year) if re.fullmatch(r"(19|20)\d\d", year) else None,
                "operator": None,
                "source_url": source_url,
                "retrieved_at": retrieved_text,
                "dataset_hash": dataset_hash,
            }
        )
        seen.add(icao)
    return results
