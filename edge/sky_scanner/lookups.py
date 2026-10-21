"""When each callword or type designator was last looked up, kept on disk.

The callword and type-photo workers look things up on websites and save the
results to BigQuery. This file remembers each key's latest status and time,
so a worker can tell what is still fresh (found: 30 days, not found: 7 days)
without querying BigQuery. A missing file is seeded once from the matching
``raw`` table (``SEED_SQL``). Writes go to a temporary file and are renamed.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

FRESH_SECONDS = {"found": 30 * 86400, "not_found": 7 * 86400}

# Latest lookup per key, by table.
SEED_SQL = {
    table: f"""
select {key} as key, status, unix_millis(fetched_at) / 1000 as fetched_at
from {{dataset}}.{table}
qualify row_number() over (partition by {key} order by fetched_at desc) = 1
"""
    for table, key in (("callword_lookups", "word"), ("aircraft_type_images", "designator"))
}


class LookupCache:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.entries: dict[str, tuple[str, float]] = {}
        self.seeded = False

    @classmethod
    def load(cls, path: str | os.PathLike) -> LookupCache:
        """Read the file; a missing or unreadable one gives an empty, unseeded cache."""
        cache = cls(path)
        try:
            data = json.loads(cache.path.read_text(encoding="utf-8"))
            cache.entries = {key: (status, float(at)) for key, (status, at) in data["entries"].items()}
            cache.seeded = bool(data["seeded"])
        except FileNotFoundError:
            pass
        except (ValueError, KeyError, TypeError):
            # Rebuilt from BigQuery by seeding; nothing here is the only copy.
            cache.entries = {}
        return cache

    def is_fresh(self, key: str, now: float | None = None) -> bool:
        entry = self.entries.get(key)
        if entry is None:
            return False
        status, fetched_at = entry
        return (time.time() if now is None else now) - fetched_at < FRESH_SECONDS.get(status, 0)

    def seed(self, rows: Iterable[dict[str, Any]]) -> int:
        """Merge ``SEED_SQL`` rows (newer entries win) and save."""
        count = 0
        for row in rows:
            self._set(row["key"], row["status"], float(row["fetched_at"]))
            count += 1
        self.seeded = True
        self.save()
        return count

    def record(self, key: str, status: str, fetched_at: float) -> None:
        self._set(key, status, fetched_at)
        self.save()

    def save(self) -> None:
        data = {
            "seeded": self.seeded,
            "entries": {key: [status, round(at, 3)] for key, (status, at) in sorted(self.entries.items())},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        with open(temporary, "w", encoding="utf-8") as out:
            json.dump(data, out, separators=(",", ":"))
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, self.path)

    def _set(self, key: str, status: str, fetched_at: float) -> None:
        existing = self.entries.get(key)
        if existing is None or fetched_at >= existing[1]:
            self.entries[key] = (status, fetched_at)
