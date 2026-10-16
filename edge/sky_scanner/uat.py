"""Normalize dump978-fa ``--json-stdout`` aircraft messages.

The decoder emits one JSON object per downlink message. Its
``metadata.received_at`` value is the decoder's Unix reception time in seconds;
``metadata.raw_timestamp`` is a hardware/raw counter and must not be used as
wall-clock time. Uplink messages are not emitted by the decoder's JSON output,
but are rejected defensively when an envelope identifies one.
"""

from __future__ import annotations

import math
import time
from typing import Any

_QUALIFIERS = {
    "adsb_icao": ("direct", "icao"),
    "adsb_other": ("direct", "uat:adsb_other"),
    "tisb_icao": ("tisb", "icao"),
    "tisb_trackfile": ("tisb", "uat:tisb_trackfile"),
    "adsr_other": ("adsr", "uat:adsr_other"),
}

_FIELD_ALIASES = {
    "pressure_altitude": "altitude_baro_ft",
    "geometric_altitude": "altitude_geom_ft",
    "ground_speed": "ground_speed_kt",
    "true_track": "track_deg",
    "vertical_velocity_barometric": "vertical_rate_baro_fpm",
    "vertical_velocity_geometric": "vertical_rate_geom_fpm",
    "callsign": "callsign",
}


def _valid_time(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def normalize_uat(payload: dict[str, Any], receiver_id: str, received_at: float | None = None) -> dict[str, Any] | None:
    """Convert a dump978-fa JSON downlink object to SnapshotAccumulator shape.

    The returned dictionary contains ``received_at``, ``receiver_id``,
    ``address``, qualifier-scoped ``address_type``, ``frequency_hz`` (978 MHz),
    a qualifier-derived ``source`` (``direct``, ``tisb``, or ``adsr``), and a
    ``fields`` mapping. All decoded payload attributes other than the address,
    qualifier, and transport metadata are retained in ``fields``; common
    aircraft fields are additionally exposed under the canonical names used by
    storage. ``None`` is returned for invalid, reserved, unknown-qualifier, or
    non-aircraft messages.

    ``metadata.received_at`` is preferred as the actual decoder reception time.
    The optional ``received_at`` argument supplies a fallback epoch time, and
    current Unix time is used only when neither is available.
    """
    if not isinstance(payload, dict):
        return None
    envelope_type = str(payload.get("type", payload.get("message_type", ""))).lower()
    if envelope_type in {"uplink", "up", "metadata", "weather", "dlp"}:
        return None

    qualifier = str(payload.get("address_qualifier", "")).strip().lower()
    identity = _QUALIFIERS.get(qualifier)
    if identity is None:
        return None
    source, address_type = identity

    address_value = payload.get("address")
    if address_value is None or isinstance(address_value, bool):
        return None
    address = str(address_value).strip().upper()
    if address.startswith("0X"):
        address = address[2:]
    if not address or any(ch not in "0123456789ABCDEF" for ch in address):
        return None
    # dump978 emits a zero-padded six-character hex string; normalize numeric
    # or abbreviated test/input forms without ever losing leading zeroes.
    if len(address) > 6:
        return None
    address = address.zfill(6)

    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    received = _valid_time(metadata.get("received_at"))
    if received is None:
        received = _valid_time(received_at)
    if received is None:
        received = time.time()

    excluded = {"address", "address_qualifier", "metadata", "type", "message_type"}
    fields = {key: value for key, value in payload.items() if key not in excluded}
    # Preserve decoded source names while adding stable, storage-facing names.
    for source_name, canonical_name in _FIELD_ALIASES.items():
        if source_name in payload:
            fields[canonical_name] = payload[source_name]
    position = payload.get("position")
    if isinstance(position, dict):
        if "lat" in position:
            fields["latitude"] = position["lat"]
        if "lon" in position:
            fields["longitude"] = position["lon"]
    state = str(payload.get("airground_state", "")).lower()
    if state == "ground":
        fields["on_ground"] = True
    elif state in {"airborne", "supersonic"}:
        fields["on_ground"] = False
    flightplan_id = payload.get("flightplan_id")
    if flightplan_id is not None:
        fields["squawk"] = flightplan_id

    return {
        "received_at": received,
        "receiver_id": str(receiver_id),
        "address": address,
        "address_type": address_type,
        "frequency_hz": 978_000_000,
        "source": source,
        "fields": fields,
    }
