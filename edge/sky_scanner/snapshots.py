"""UTC-aligned aircraft snapshot aggregation.

``SnapshotAccumulator`` accepts normalized observation dictionaries containing
``received_at`` (Unix seconds), ``receiver_id``, ``address``, ``address_type``,
``frequency_hz`` and a ``fields`` mapping. Each observation contributes to a
half-open bucket of ``bucket_seconds``. ``flush_ready(now)`` emits completed
buckets; ``flush_all()`` emits all pending buckets for orderly shutdown.

Returned snapshots contain identity/provenance, bucket start, first/last
reception times, valid message count, and a ``fields`` mapping. Each field is
``{"value": ..., "timestamp": ..., "source": ...}``. The latest value is
carried into later snapshots for at most ``carry_seconds``; its original
timestamp and source are preserved. Scalar field values use the observation's
optional ``source``; callers may also supply the expanded field mapping shape.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

Identity = tuple[str, str, str, int]
BucketKey = tuple[str, str, str, int, int]


@dataclass
class _Bucket:
    first_received_at: float
    last_received_at: float
    message_count: int = 0
    fields: dict[str, dict[str, Any]] = field(default_factory=dict)


class SnapshotAccumulator:
    """Aggregate observations into snapshots, without producing empty rows."""

    def __init__(self, bucket_seconds: int = 15, carry_seconds: int = 60):
        if bucket_seconds <= 0 or carry_seconds < 0:
            raise ValueError("bucket_seconds must be positive and carry_seconds nonnegative")
        self.bucket_seconds = bucket_seconds
        self.carry_seconds = carry_seconds
        self._buckets: dict[BucketKey, _Bucket] = {}
        self._carry: dict[Identity, dict[str, dict[str, Any]]] = {}
        # A global watermark bounds memory while rejecting all late arrivals.
        self._flushed_through: int | None = None

    def add_observation(self, observation: dict[str, Any]) -> None:
        """Add one valid normalized observation; late observations raise ValueError."""
        try:
            t = float(observation["received_at"])
            receiver = str(observation["receiver_id"])
            address = str(observation["address"])
            address_type = str(observation["address_type"])
            frequency = int(observation["frequency_hz"])
            fields = observation["fields"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("observation is missing valid normalized fields") from exc
        if not math.isfinite(t) or not isinstance(fields, dict):
            raise ValueError("received_at must be finite and fields must be a mapping")
        start = math.floor(t / self.bucket_seconds) * self.bucket_seconds
        identity = (receiver, address_type, address, frequency)
        if self._flushed_through is not None and start <= self._flushed_through:
            raise ValueError("observation belongs to an already flushed bucket")
        key = (*identity, start)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = self._buckets[key] = _Bucket(t, t)
        bucket.first_received_at = min(bucket.first_received_at, t)
        bucket.last_received_at = max(bucket.last_received_at, t)
        bucket.message_count += 1
        default_source = observation.get("source")
        for name, raw in fields.items():
            if isinstance(raw, dict) and "value" in raw:
                timestamp = float(raw.get("timestamp", t))
                if not math.isfinite(timestamp) or timestamp > t:
                    raise ValueError("field timestamp must be finite and no later than received_at")
                item = {
                    "value": raw["value"],
                    "timestamp": timestamp,
                    "source": raw.get("source", default_source),
                }
            else:
                item = {"value": raw, "timestamp": t, "source": default_source}
            prev = bucket.fields.get(str(name))
            if prev is None or item["timestamp"] >= prev["timestamp"]:
                bucket.fields[str(name)] = item

    def flush_ready(self, now: float) -> list[dict[str, Any]]:
        """Emit buckets whose end is at or before ``now``, in chronological order."""
        now = float(now)
        ready = sorted(
            (k for k in self._buckets if k[-1] + self.bucket_seconds <= now),
            key=lambda k: (k[-1], *k[:-1]),
        )
        rows = [self._emit(key) for key in ready]
        self._cleanup_carry(now)
        return rows

    def flush_all(self) -> list[dict[str, Any]]:
        """Emit every pending bucket in order, including a partial final bucket."""
        keys = sorted(self._buckets, key=lambda k: (k[-1], *k[:-1]))
        rows = [self._emit(key) for key in keys]
        if keys:
            self._cleanup_carry(keys[-1][-1] + self.bucket_seconds)
        return rows

    def _emit(self, key: BucketKey) -> dict[str, Any]:
        receiver, address_type, address, frequency, start = key
        bucket = self._buckets.pop(key)
        identity = (receiver, address_type, address, frequency)
        carry = self._carry.setdefault(identity, {})
        for name, value in bucket.fields.items():
            previous = carry.get(name)
            if previous is None or value["timestamp"] >= previous["timestamp"]:
                carry[name] = value
        cutoff = start + self.bucket_seconds - self.carry_seconds
        fields = {name: dict(value) for name, value in carry.items() if value["timestamp"] >= cutoff}
        self._carry[identity] = fields.copy()
        self._flushed_through = max(start, self._flushed_through if self._flushed_through is not None else start)
        return {
            "bucket_start": start,
            "receiver_id": receiver,
            "address_type": address_type,
            "address": address,
            "frequency_hz": frequency,
            "first_received_at": bucket.first_received_at,
            "last_received_at": bucket.last_received_at,
            "message_count": bucket.message_count,
            "fields": fields,
        }

    def _cleanup_carry(self, now: float) -> None:
        """Discard expired cached fields and empty identities to bound memory."""
        expired_before = now - self.carry_seconds - self.bucket_seconds
        for identity, values in list(self._carry.items()):
            fresh = {name: value for name, value in values.items() if value["timestamp"] >= expired_before}
            if fresh:
                self._carry[identity] = fresh
            else:
                del self._carry[identity]
