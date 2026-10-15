"""Parse and locate the free OpenSky Network aircraft database snapshots.

OpenSky publishes crowdsourced monthly snapshots named
``metadata/aircraft-database-complete-YYYY-MM.csv`` (comma-delimited, single
quote as the quote character). The data is provided "as is" without a license
or guarantees, so records are stored under their own source name and never
replace FAA registry values. Values are attributable to OpenSky, not verified.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import urllib.request
from datetime import datetime, timezone
from typing import Any
from xml.etree import ElementTree

from .enrichment import normalize_icao

SOURCE_NAME = "opensky"
BUCKET_URL = "https://s3.opensky-network.org/data-samples"
LIST_URL = BUCKET_URL + "?list-type=2&prefix=metadata/aircraft-database-complete-"
MAX_DATABASE_BYTES = 400 * 1024 * 1024
_SNAPSHOT_KEY = re.compile(r"^metadata/aircraft-database-complete-(\d{4})-(\d{2})\.csv$")
_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
_REQUIRED = ("icao24", "registration", "model", "typecode", "operator", "owner")


def latest_snapshot_url(listing: bytes) -> str:
    """Return the newest complete monthly snapshot URL from an S3 listing."""
    root = ElementTree.fromstring(listing)
    keys = [element.text or "" for element in root.iter(_S3_NS + "Key")]
    snapshots = sorted(key for key in keys if _SNAPSHOT_KEY.match(key))
    if not snapshots:
        raise ValueError("no OpenSky complete aircraft database snapshot listed")
    return f"{BUCKET_URL}/{snapshots[-1]}"


def find_latest_snapshot(timeout: int = 60) -> str:
    with urllib.request.urlopen(LIST_URL, timeout=timeout) as response:
        return latest_snapshot_url(response.read(10 * 1024 * 1024))


def download_snapshot(url: str, timeout: int = 300) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        data = response.read(MAX_DATABASE_BYTES + 1)
    if len(data) > MAX_DATABASE_BYTES:
        raise ValueError(f"OpenSky database exceeds {MAX_DATABASE_BYTES} bytes")
    if not data:
        raise ValueError("OpenSky database download was empty")
    return data


def _clean(value: Any) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text or None


def parse_opensky_database(
    data: bytes, *, source_url: str, retrieved_at: datetime | None = None
) -> list[dict[str, Any]]:
    """Parse an OpenSky complete snapshot into aircraft metadata records.

    Rows without a valid 24-bit address, or without any registration, model,
    type code, operator, or owner, are omitted. ``registered_owner`` holds
    OpenSky's crowdsourced ``owner`` value; it is not an official registry.
    """
    retrieved = (retrieved_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    retrieved_text = retrieved.isoformat().replace("+00:00", "Z")
    dataset_hash = hashlib.sha256(data).hexdigest()
    reader = csv.reader(io.StringIO(data.decode("utf-8", errors="replace")), quotechar="'")
    try:
        header = [column.strip().strip("'").lstrip("﻿") for column in next(reader)]
    except StopIteration as exc:
        raise ValueError("OpenSky database is empty") from exc
    missing = [name for name in _REQUIRED if name not in header]
    if missing:
        raise ValueError("OpenSky database missing column(s): " + ", ".join(missing))
    index = {name: i for i, name in enumerate(header)}
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in reader:
        if len(row) != len(header):
            continue

        def get(name: str) -> str | None:
            position = index.get(name)
            return _clean(row[position]) if position is not None else None

        address = normalize_icao(get("icao24"))
        if not address or address in seen:
            continue
        manufacturer = get("manufacturerName") or get("manufacturerIcao")
        record = {
            "address_type": "icao",
            "icao_address": address,
            "registration": get("registration"),
            "manufacturer": manufacturer,
            "model": get("model"),
            "aircraft_type": get("icaoAircraftClass"),
            "icao_type_code": get("typecode"),
            "registered_owner": get("owner"),
            "operator": get("operator"),
            "operator_icao": get("operatorIcao"),
            "country": get("country"),
        }
        if not any(record[key] for key in ("registration", "model", "icao_type_code",
                                           "operator", "operator_icao", "registered_owner")):
            continue
        record.update(source_url=source_url, retrieved_at=retrieved_text, dataset_hash=dataset_hash)
        results.append(record)
        seen.add(address)
    return results
