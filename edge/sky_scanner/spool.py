"""On-disk queue of row batches waiting to be sent to BigQuery.

Each batch is one JSON Lines file named by the time it was written and the
table it belongs to. A file is written to a temporary name, flushed to disk
and then renamed, so a crash never leaves a partial batch. Batches are sent
oldest first and deleted only after BigQuery accepts them; a failed send
leaves the batch (and every later one) in place for the next attempt.

A batch BigQuery refuses outright (``RowsRejected``) or that cannot be read
is renamed to ``*.rejected`` and skipped, so it never blocks the queue; it
stays on disk for inspection and manual replay. When the spool grows past
``max_bytes``, the oldest files are dropped with an error log.

One thread may ``put`` while another drains; a lock covers file changes but
not the send itself.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .bq import RowsRejected

log = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 1024**3
_NAME = re.compile(r"^\d{20}-\d{6}\.([a-z_]+)\.jsonl$")


class Spool:
    def __init__(self, directory: str | os.PathLike, *, max_bytes: int = DEFAULT_MAX_BYTES):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self._sequence = 0
        self._lock = threading.Lock()
        # Leftovers from a crash mid-write were never renamed, so never sent.
        for leftover in self.directory.glob("*.tmp"):
            leftover.unlink()

    def put(self, table: str, rows: list[dict[str, Any]]) -> Path | None:
        """Durably queue ``rows`` (already in BigQuery JSON form) for ``table``."""
        if not rows:
            return None
        if not re.fullmatch(r"[a-z_]+", table):
            raise ValueError(f"invalid table name {table!r}")
        data = b"".join(json.dumps(row, separators=(",", ":")).encode() + b"\n" for row in rows)
        with self._lock:
            self._sequence = (self._sequence + 1) % 1_000_000
            path = self.directory / f"{time.time_ns():020d}-{self._sequence:06d}.{table}.jsonl"
            temporary = path.with_name(path.name + ".tmp")
            with open(temporary, "wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, path)
            self._sync_directory()
            self._enforce_cap()
        return path

    def pending(self) -> list[Path]:
        """Batches waiting to be sent, oldest first."""
        return sorted(p for p in self.directory.glob("*.jsonl") if _NAME.match(p.name))

    def size(self) -> int:
        return sum(p.stat().st_size for p in self._files())

    def drain(self, send: Callable[[str, list[dict[str, Any]]], Any], *, max_batches: int | None = None) -> int:
        """Send waiting batches in order; returns the number of rows sent.

        Stops at the first failure (other than a rejection) and re-raises it,
        so later batches never overtake an earlier one.
        """
        sent = 0
        for path in self.pending()[:max_batches]:
            table = _NAME.match(path.name).group(1)
            with self._lock:
                if not path.exists():  # dropped by the size cap meanwhile
                    continue
                try:
                    rows = [json.loads(line) for line in path.read_bytes().splitlines() if line.strip()]
                except (OSError, ValueError) as exc:
                    self._reject(path, f"unreadable: {exc}")
                    continue
            try:
                send(table, rows)
            except RowsRejected as exc:
                with self._lock:
                    self._reject(path, str(exc))
                continue
            with self._lock:
                path.unlink(missing_ok=True)
            sent += len(rows)
        if sent:
            self._sync_directory()
        return sent

    def _files(self) -> list[Path]:
        return sorted(p for p in self.directory.iterdir() if p.suffix in (".jsonl", ".rejected"))

    def _reject(self, path: Path, reason: str) -> None:
        target = path.with_name(path.name + ".rejected")
        if not path.exists():
            return
        os.replace(path, target)
        log.error("spool batch %s rejected and set aside as %s: %s", path.name, target.name, reason)

    def _enforce_cap(self) -> None:
        files = self._files()
        total = sum(p.stat().st_size for p in files)
        # Keep at least the newest file, even if it alone exceeds the cap.
        for path in files[:-1]:
            if total <= self.max_bytes:
                break
            size = path.stat().st_size
            lines = path.read_bytes().count(b"\n")
            path.unlink()
            total -= size
            log.error("spool over %d bytes: dropped %s (%d rows)", self.max_bytes, path.name, lines)

    def _sync_directory(self) -> None:
        descriptor = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
