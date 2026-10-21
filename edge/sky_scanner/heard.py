"""Which aircraft the receiver has heard, kept by ingest for enrichment.

``heard.json`` holds one entry per address type and address: when it was
first and last heard (Unix seconds) and the callsigns it used, each with when
it was last used. Ingest updates it from every snapshot it writes and saves
it about once a minute; enrichment reads it to decide what to look up, so it
never has to query BigQuery for that.

Every address is kept (a few thousand, growing slowly); callsigns unused for
``callsign_days`` are pruned. The file is written to a temporary name and
renamed, so a reader never sees a partial file. Methods are thread-safe.

When the file does not exist yet, it is seeded once from BigQuery ``raw``
(``SEED_SQL``) and merged with whatever was heard meanwhile; ``seeded``
records that this happened.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FORMAT_VERSION = 1
CALLSIGN_DAYS = 30

# One row per aircraft: duplicate deliveries removed, as in dbt staging.
SEED_SQL = """
with snapshots as (
    select address_type, upper(address) as address, first_received_at, last_received_at,
           nullif(upper(trim(callsign)), '') as callsign
    from {dataset}.aircraft_snapshots
    qualify row_number() over (
        partition by bucket_start, receiver_id, address_type, address, frequency_hz
        order by written_at desc
    ) = 1
),
callsigns as (
    select address_type, address, callsign, max(last_received_at) as last_used
    from snapshots
    where callsign is not null and last_received_at >= @callsigns_since
    group by address_type, address, callsign
)
select s.address_type, s.address,
       unix_millis(min(s.first_received_at)) / 1000 as first_seen,
       unix_millis(max(s.last_received_at)) / 1000 as last_seen,
       array(
           select as struct c.callsign, unix_millis(c.last_used) / 1000 as last_used
           from callsigns as c
           where c.address_type = s.address_type and c.address = s.address
       ) as callsigns
from snapshots as s
group by s.address_type, s.address
"""


@dataclass
class Aircraft:
    first_seen: float
    last_seen: float
    callsigns: dict[str, float] = field(default_factory=dict)

    def merge(self, other: Aircraft) -> None:
        self.first_seen = min(self.first_seen, other.first_seen)
        self.last_seen = max(self.last_seen, other.last_seen)
        for callsign, used in other.callsigns.items():
            self.callsigns[callsign] = max(used, self.callsigns.get(callsign, used))


def _seconds(value: Any) -> float:
    return value.timestamp() if isinstance(value, datetime) else float(value)


def _callsign(fields: dict[str, Any]) -> str | None:
    item = fields.get("callsign")
    value = item.get("value") if isinstance(item, dict) else item
    text = str(value or "").strip().upper()
    return text or None


class Heard:
    def __init__(self, path: str | os.PathLike, *, callsign_days: int = CALLSIGN_DAYS):
        self.path = Path(path)
        self.callsign_days = callsign_days
        self.aircraft: dict[tuple[str, str], Aircraft] = {}
        self.seeded = False
        self._lock = threading.RLock()

    @classmethod
    def load(cls, path: str | os.PathLike, **kwargs) -> Heard:
        """Read the file; a missing file gives an empty, unseeded instance."""
        heard = cls(path, **kwargs)
        try:
            data = json.loads(heard.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return heard
        if data.get("version") != FORMAT_VERSION:
            raise ValueError(f"{path}: unsupported heard.json version {data.get('version')!r}")
        heard.seeded = bool(data.get("seeded"))
        for item in data["aircraft"]:
            heard.aircraft[(item["address_type"], item["address"])] = Aircraft(
                item["first_seen"], item["last_seen"], dict(item["callsigns"])
            )
        return heard

    def update(self, snapshots: Iterable[dict[str, Any]]) -> None:
        """Record snapshots as the accumulator emits them (Unix-second times)."""
        with self._lock:
            for row in snapshots:
                key = (row["address_type"], row["address"].upper())
                last = _seconds(row["last_received_at"])
                entry = Aircraft(_seconds(row["first_received_at"]), last)
                callsign = _callsign(row.get("fields") or {})
                if callsign:
                    entry.callsigns[callsign] = last
                self._merge(key, entry)

    def seed(self, rows: Iterable[dict[str, Any]]) -> int:
        """Merge aircraft from ``SEED_SQL`` rows; returns how many were read."""
        entries = [
            (
                (row["address_type"], row["address"].upper()),
                Aircraft(
                    float(row["first_seen"]),
                    float(row["last_seen"]),
                    {c["callsign"]: float(c["last_used"]) for c in row["callsigns"] or []},
                ),
            )
            for row in rows
        ]
        with self._lock:
            for key, entry in entries:
                self._merge(key, entry)
            self.seeded = True
        return len(entries)

    def seed_params(self, now: float | None = None) -> dict[str, Any]:
        cutoff = (time.time() if now is None else now) - self.callsign_days * 86400
        return {"callsigns_since": datetime.fromtimestamp(cutoff, UTC)}

    def prune(self, now: float | None = None) -> None:
        """Forget callsigns not used for ``callsign_days``; addresses are kept."""
        cutoff = (time.time() if now is None else now) - self.callsign_days * 86400
        with self._lock:
            for entry in self.aircraft.values():
                entry.callsigns = {c: used for c, used in entry.callsigns.items() if used >= cutoff}

    def heard_since(self, since: float) -> list[tuple[str, str]]:
        with self._lock:
            return sorted(key for key, entry in self.aircraft.items() if entry.last_seen >= since)

    def save(self) -> None:
        with self._lock:
            data = self._to_json()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        with open(temporary, "w", encoding="utf-8") as out:
            json.dump(data, out, separators=(",", ":"))
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, self.path)

    def _to_json(self) -> dict[str, Any]:
        return {
            "version": FORMAT_VERSION,
            "seeded": self.seeded,
            "saved_at": round(time.time(), 3),
            "aircraft": [
                {
                    "address_type": address_type,
                    "address": address,
                    "first_seen": round(entry.first_seen, 3),
                    "last_seen": round(entry.last_seen, 3),
                    "callsigns": {c: round(used, 3) for c, used in sorted(entry.callsigns.items())},
                }
                for (address_type, address), entry in sorted(self.aircraft.items())
            ],
        }

    def _merge(self, key: tuple[str, str], entry: Aircraft) -> None:
        existing = self.aircraft.get(key)
        if existing is None:
            self.aircraft[key] = entry
        else:
            existing.merge(entry)
