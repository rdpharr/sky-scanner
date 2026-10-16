"""Identity of recently heard aircraft, for the enrichment worker.

Aggregates recent snapshots per address, attaches FAA (official), tar1090-db,
OpenSky (crowdsourced) and plane-alert-db metadata, and resolves each
aircraft's ICAO type designator, readable model name and kind (military,
airline or other). The worker uses this to choose which aircraft types need a
Wikipedia photo, military first. The public classification lives in dbt
(dim_aircraft); nothing here is written back to the database.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from . import military

SUMMARY_SQL = """
WITH a AS (
    SELECT address_type, address,
           min(first_received_at) AS first_seen, max(last_received_at) AS last_seen,
           array_agg(DISTINCT frequency_hz) AS bands, count(*) AS snapshots,
           sum(message_count) AS messages, max(altitude_baro_ft) AS max_alt,
           (array_agg(btrim(callsign) ORDER BY bucket_start DESC)
                FILTER (WHERE btrim(coalesce(callsign, '')) <> ''))[1] AS callsign
    FROM aircraft_snapshots
    WHERE %(since)s::timestamptz IS NULL OR last_received_at >= %(since)s::timestamptz
    GROUP BY address_type, address
)
SELECT a.address, a.address_type, a.first_seen, a.last_seen, a.bands, a.snapshots,
       a.messages, a.max_alt, a.callsign,
       f.registration, f.manufacturer, f.model, f.registered_owner,
       o.registration, o.model, o.icao_type_code, o.operator, o.operator_icao,
       o.registered_owner, o.country,
       p.operator, p.model, p.icao_type_code, p.category, p.tags, p.registration, mc.name,
       t.registration, t.icao_type_code, t.model, t.registered_owner, t.tags
FROM a
LEFT JOIN LATERAL (
    SELECT * FROM aircraft_metadata m
    WHERE m.source_name = 'faa_registry' AND m.registry_status = 'present'
      AND m.address_type = a.address_type AND m.address = a.address
    ORDER BY m.retrieved_at DESC, m.id DESC LIMIT 1
) f ON true
LEFT JOIN LATERAL (
    SELECT * FROM aircraft_metadata m
    WHERE m.source_name = 'opensky' AND m.registry_status = 'present'
      AND m.address_type = a.address_type AND m.address = a.address
    ORDER BY m.retrieved_at DESC, m.id DESC LIMIT 1
) o ON true
LEFT JOIN LATERAL (
    SELECT * FROM aircraft_metadata m
    WHERE m.source_name = 'plane_alert_mil'
      AND m.address_type = a.address_type AND m.address = a.address
    ORDER BY m.retrieved_at DESC, m.id DESC LIMIT 1
) p ON p.registry_status = 'present'
LEFT JOIN military_operator_codes mc
    ON a.callsign ~ '^[A-Z]{3}[0-9]' AND mc.code = left(a.callsign, 3)
LEFT JOIN LATERAL (
    SELECT * FROM aircraft_metadata m
    WHERE m.source_name = 'tar1090_db' AND m.registry_status = 'present'
      AND m.address_type = a.address_type AND m.address = a.address
    ORDER BY m.retrieved_at DESC, m.id DESC LIMIT 1
) t ON true
ORDER BY a.first_seen
"""

# Readable names for common designators; others fall back to ICAO Doc 8643.
TYPE_NAMES = {
    "A20N": "Airbus A320neo",
    "A21N": "Airbus A321neo",
    "A319": "Airbus A319",
    "A320": "Airbus A320",
    "A321": "Airbus A321",
    "A332": "Airbus A330-200",
    "A333": "Airbus A330-300",
    "A339": "Airbus A330-900neo",
    "A359": "Airbus A350-900",
    "A35K": "Airbus A350-1000",
    "B737": "Boeing 737-700",
    "B738": "Boeing 737-800",
    "B739": "Boeing 737-900/900ER",
    "B38M": "Boeing 737 MAX 8",
    "B39M": "Boeing 737 MAX 9",
    "B744": "Boeing 747-400",
    "B748": "Boeing 747-8",
    "B752": "Boeing 757-200",
    "B753": "Boeing 757-300",
    "B763": "Boeing 767-300",
    "B772": "Boeing 777-200",
    "B77L": "Boeing 777-200LR/F",
    "B77W": "Boeing 777-300ER",
    "B788": "Boeing 787-8",
    "B789": "Boeing 787-9",
    "B78X": "Boeing 787-10",
    "BCS1": "Airbus A220-100",
    "BCS3": "Airbus A220-300",
    "CRJ2": "Bombardier CRJ200",
    "CRJ7": "Bombardier CRJ700",
    "CRJ9": "Bombardier CRJ900",
    "E135": "Embraer ERJ-135",
    "E145": "Embraer ERJ-145",
    "E170": "Embraer E170",
    "E75L": "Embraer E175",
    "E75S": "Embraer E175",
    "E190": "Embraer E190",
    "E295": "Embraer E195-E2",
    "DH8B": "De Havilland Dash 8-200",
    "DH8D": "De Havilland Dash 8-400",
    "C172": "Cessna 172 Skyhawk",
    "C182": "Cessna 182",
    "C208": "Cessna 208 Caravan",
    "C25A": "Cessna Citation CJ2",
    "C25B": "Cessna Citation CJ3",
    "C25C": "Cessna Citation CJ4",
    "C56X": "Cessna Citation Excel/XLS",
    "C68A": "Cessna Citation Latitude",
    "C700": "Cessna Citation Longitude",
    "C750": "Cessna Citation X",
    "CL30": "Bombardier Challenger 300",
    "CL35": "Bombardier Challenger 350",
    "CL60": "Bombardier Challenger 600 series",
    "E55P": "Embraer Phenom 300",
    "E545": "Embraer Legacy 450/Praetor 500",
    "E550": "Embraer Legacy 500/Praetor 600",
    "F2TH": "Dassault Falcon 2000",
    "F900": "Dassault Falcon 900",
    "FA50": "Dassault Falcon 50",
    "FA7X": "Dassault Falcon 7X",
    "GA5C": "Gulfstream G500",
    "GA6C": "Gulfstream G600",
    "GLF4": "Gulfstream G450/GIV",
    "GLF5": "Gulfstream G550/GV",
    "GLF6": "Gulfstream G650",
    "GLEX": "Bombardier Global Express",
    "HA4T": "Hawker 4000",
    "H25B": "Hawker 800/BAe 125",
    "HDJT": "HondaJet",
    "LJ45": "Learjet 45",
    "LJ60": "Learjet 60",
    "SF50": "Cirrus SF50 Vision Jet",
    "PC12": "Pilatus PC-12",
    "PC24": "Pilatus PC-24",
    "TBM7": "Daher TBM 700",
    "TBM9": "Daher TBM 900 series",
    "EPIC": "Epic E1000",
    "BE30": "Beech King Air 350",
    "BE20": "Beech King Air 200",
    "BE36": "Beech Bonanza 36",
    "DA40": "Diamond DA40",
    "DA42": "Diamond DA42",
    "E300": "Extra 300",
    "P28A": "Piper PA-28 Cherokee/Archer",
    "P28B": "Piper PA-28-235 Cherokee",
    "P28R": "Piper PA-28R Arrow",
    "PA44": "Piper PA-44 Seminole",
    "PA46": "Piper PA-46 Malibu",
    "SR22": "Cirrus SR22",
    "S22T": "Cirrus SR22T",
    "T206": "Cessna T206 Turbo Stationair",
    "C240": "Cessna TTx",
    "M20P": "Mooney M20",
    "C414": "Cessna 414",
    "H500": "MD 500 / Hughes 369",
    "EC30": "Airbus H130 / EC130",
    "H60": "Sikorsky H-60 Black Hawk family",
    "AS50": "Airbus AS350 / H125",
}

# FAA registry model text -> designator, for aircraft OpenSky has no type code for.
FAA_MODEL_TYPES = [
    (r"737-8$", "B38M"),
    (r"737-9$", "B39M"),
    (r"737-7$", "B37M"),
    (r"737-7..$", "B737"),
    (r"737-8..$", "B738"),
    (r"737-9..(ER)?$", "B739"),
    (r"737-800$", "B738"),
    (r"757-2..$", "B752"),
    (r"757-3..$", "B753"),
    (r"767-3..F?$", "B763"),
    (r"777-2..$", "B772"),
    (r"787-8$", "B788"),
    (r"787-9$", "B789"),
    (r"787-10$", "B78X"),
    (r"A321-2.1NX|A321-2.3NY|A321-2..N", "A21N"),
    (r"A321-2..$", "A321"),
    (r"A320-2.1N|A320-2..N$", "A20N"),
    (r"A320-2..$", "A320"),
    (r"A319-1..$", "A319"),
    (r"A350-941", "A359"),
    (r"BD-500-1A11", "BCS3"),
    (r"BD-500-1A10", "BCS1"),
    (r"BD-100-1A10", "CL30"),
    (r"CL-600-2C10", "CRJ7"),
    (r"CL-600-2B19", "CRJ2"),
    (r"ERJ 170-200", "E75L"),
    (r"EMB-545", "E545"),
    (r"EMB-505", "E55P"),
    (r"EMB-135", "E135"),
    (r"EMB-145", "E145"),
    (r"GIV-X|G450", "GLF4"),
    (r"GV-SP|G550", "GLF5"),
    (r"GVII-G500", "GA5C"),
    (r"GVI|G650", "GLF6"),
    (r"TBM 700", "TBM7"),
    (r"PC-12", "PC12"),
    (r"E1000", "EPIC"),
    (r"DHC-8-2", "DH8B"),
    (r"680A", "C68A"),
    (r"^750$", "C750"),
    (r"525C", "C25C"),
    (r"525A", "C25A"),
    (r"560XL", "C56X"),
    (r"^172", "C172"),
    (r"^208B?", "C208"),
    (r"SR22T", "S22T"),
    (r"^SR22", "SR22"),
    (r"SF50", "SF50"),
    (r"HA-420", "HDJT"),
    (r"^45$", "LJ45"),
    (r"^60$", "LJ60"),
    (r"FALCON 2000", "F2TH"),
    (r"FALCON 900", "F900"),
    (r"FALCON 50", "FA50"),
    (r"FALCON 7X", "FA7X"),
    (r"^4000$", "HA4T"),
    (r"B300", "BE30"),
    (r"G36", "BE36"),
    (r"DA 40", "DA40"),
    (r"EA 300", "E300"),
    (r"PA-28R", "P28R"),
    (r"PA-28-235", "P28B"),
    (r"PA-28", "P28A"),
    (r"PA 46", "PA46"),
    (r"TU206", "T206"),
    (r"369", "H500"),
    (r"EC 130", "EC30"),
]

AIRLINE_CALLSIGN = re.compile(
    r"^(AAL|SWA|UAL|DAL|JBU|ASA|FFT|NKS|AAY|SKW|ENY|RPA|QXE|HAL|FDX|UPS|ACA|WJA|POE|"
    r"AFR|KLM|BAW|VIR|DLH|THY|KAL|AAR|JAL|ANA|CPA|QFA|ANZ|VOI|AMX|VIV|VOC|SCX|MXY|"
    r"BRE|ASH|GJS|EDV|CKS|ATN|ABX|GTI|WSW|FLE|ROU|SWG|TSC|EDW|TAP|THT|ACI|MXA)\d"
)
AIRLINE_NAME = re.compile(
    r"AIR ?LINES|AIRWAYS|AIRLINE|FEDERAL EXPRESS|UNITED PARCEL|"
    r"KOREAN AIR|AIR FRANCE|AIR CANADA|PORTER|SWOOP|TAP PORTUGAL",
    re.I,
)
LESSOR = re.compile(r"BANK|TRUST|ASSET MANAGEMENT|LEASING", re.I)
AIRLINER_TYPES = (
    "A2",
    "A3",
    "B7",
    "B3",
    "E17",
    "E19",
    "E75",
    "E29",
    "E13",
    "E14",
    "BCS",
    "CRJ",
    "MD8",
    "DH8",
    "AT7",
    "AT4",
)


def parse_doc8643(data: bytes) -> dict[str, dict[str, str]]:
    """Designator -> first Doc 8643 row (Description like L2J, WTC, names)."""
    types: dict[str, dict[str, str]] = {}
    for row in csv.DictReader(io.StringIO(data.decode("utf-8", errors="replace"))):
        designator = (row.get("Designator") or "").strip()
        if designator:
            types.setdefault(designator, row)
    return types


def faa_type(model: str | None) -> str | None:
    text = (model or "").strip().upper()
    if not text:
        return None
    for pattern, designator in FAA_MODEL_TYPES:
        if re.search(pattern, text):
            return designator
    return None


def aircraft_class(designator: str | None, types: dict[str, dict[str, str]]) -> str:
    if not designator:
        return "Unidentified"
    info = types.get(designator)
    if info is None:
        if designator.startswith(AIRLINER_TYPES):
            return "Narrow-body / regional jet"
        return "Other"
    description, wtc = info.get("Description", ""), info.get("WTC", "")
    if description.startswith(("H", "G")):
        return "Helicopter"
    engine = description[-1:] if description else ""
    if engine == "P":
        return "Piston"
    if engine == "T":
        return "Turboprop"
    if engine == "J":
        if wtc in ("H", "J"):
            return "Wide-body jet"
        if designator.startswith(AIRLINER_TYPES):
            return "Narrow-body / regional jet"
        return "Business jet"
    return "Other"


def type_name(designator: str | None, types: dict[str, dict[str, str]], fallback: str) -> str:
    if designator in TYPE_NAMES:
        return TYPE_NAMES[designator]
    info = types.get(designator or "")
    if info:
        maker = (info.get("ManufacturerCode") or "").title()
        return f"{maker} {info.get('ModelFullName', '')}".strip()
    return fallback or "Unidentified"


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value


def military_evidence(
    address: str, plane_alert: dict | None, operator_code_name: str | None, tar1090: dict | None = None
) -> list[str]:
    """Why an aircraft counts as military (empty list: not military)."""
    evidence = []
    if address.upper().startswith(("AE", "AF")):
        evidence.append("US military ICAO address block")
    if plane_alert:
        evidence.append("plane-alert-db military list")
    if tar1090 and "military" in (tar1090.get("tags") or "").split(", "):
        evidence.append("tar1090-db military flag")
    if operator_code_name:
        evidence.append(f"military operator code ({operator_code_name})")
    return evidence


def identify(
    address: str,
    callsign: str | None,
    faa: dict | None,
    opensky: dict | None,
    types: dict[str, dict[str, str]],
    plane_alert: dict | None = None,
    operator_code_name: str | None = None,
    tar1090: dict | None = None,
) -> dict[str, Any]:
    """Resolve display identity from FAA (preferred) and OpenSky metadata dicts.

    Each metadata dict may hold registration, manufacturer, model,
    registered_owner, icao_type_code, operator, operator_icao and country.
    """
    faa = faa or {}
    opensky = opensky or {}
    tar1090 = tar1090 or {}
    plane_alert = {k: (military._clean(v) if isinstance(v, str) else v) for k, v in (plane_alert or {}).items()}
    miscode = "miscode" in (tar1090.get("tags") or "").split(", ") or any(
        str(v or "").lower().startswith("miscode") for v in tar1090.values()
    )
    if miscode:
        tar1090 = {"tags": "miscode"}
    faa_reg, faa_model = faa.get("registration"), faa.get("model")
    faa_desc = " ".join(part for part in (faa.get("manufacturer"), faa_model) if part)
    os_reg, os_model, os_type = (opensky.get("registration"), opensky.get("model"), opensky.get("icao_type_code"))
    t_reg, t_type, t_model = (tar1090.get("registration"), tar1090.get("icao_type_code"), tar1090.get("model"))
    # FAA is official; tar1090-db is current; OpenSky's snapshot is older.
    designator = (faa_type(faa_model) if faa_reg else None) or t_type or os_type or plane_alert.get("icao_type_code")
    if faa_reg:
        source = "F"
    elif t_reg or t_type:
        source = "T"
    elif os_reg or os_model or os_type:
        source = "O"
    elif plane_alert.get("registration") or plane_alert.get("model"):
        source = "M"
    else:
        source = "-"
    owner = faa.get("registered_owner") or tar1090.get("registered_owner") or opensky.get("registered_owner") or ""
    operator = opensky.get("operator") or opensky.get("operator_icao") or ""
    cls = aircraft_class(designator, types)
    evidence = military_evidence(address, plane_alert or None, operator_code_name, tar1090)
    service = military.us_callsign_service(address, callsign) if evidence else None
    if service:
        evidence.append(f"callsign convention ({service})")
    if evidence:
        operator = plane_alert.get("operator") or operator_code_name or service or operator
    airline = bool(
        not evidence
        and (
            AIRLINE_CALLSIGN.match(callsign or "")
            or AIRLINE_NAME.search(f"{owner} {operator}")
            or (cls in ("Narrow-body / regional jet", "Wide-body jet") and (LESSOR.search(owner) or not owner))
        )
    )
    fallback = faa_desc or t_model or os_model or plane_alert.get("model") or ""
    if miscode:
        model = "Invalid transponder address (miscode)"
    elif evidence and plane_alert.get("model"):
        # For military airframes plane-alert-db's type name is more specific.
        model = plane_alert["model"]
    else:
        model = type_name(designator, types, fallback)
    return {
        "reg": faa_reg or t_reg or os_reg or plane_alert.get("registration") or "",
        "tc": designator or "",
        "model": model,
        "acft": fallback,
        "op": operator,
        "owner": owner,
        "country": opensky.get("country") or "",
        "src": source,
        "class": cls,
        "kind": "Military" if evidence else ("Airline" if airline else "Other"),
        "military_evidence": evidence,
        "miscode": miscode,
    }


def summarize(rows: Iterable[tuple], types: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    """Turn SUMMARY_SQL rows into one record per aircraft."""
    result = []
    for (
        address,
        address_type,
        first_seen,
        last_seen,
        bands,
        snapshots,
        messages,
        max_alt,
        callsign,
        faa_reg,
        faa_mfr,
        faa_model,
        faa_owner,
        os_reg,
        os_model,
        os_type,
        os_operator,
        os_operator_icao,
        os_owner,
        os_country,
        *military_columns,
    ) in rows:
        (
            pa_operator,
            pa_model,
            pa_type,
            pa_category,
            pa_tags,
            pa_reg,
            operator_code_name,
            t_reg,
            t_type,
            t_model,
            t_owner,
            t_tags,
        ) = (list(military_columns) + [None] * 12)[:12]
        tar1090 = (
            {
                "registration": t_reg,
                "icao_type_code": t_type,
                "model": t_model,
                "registered_owner": t_owner,
                "tags": t_tags,
            }
            if any((t_reg, t_type, t_model, t_tags))
            else None
        )
        plane_alert = (
            {
                "operator": pa_operator,
                "model": pa_model,
                "icao_type_code": pa_type,
                "category": pa_category,
                "tags": pa_tags,
                "registration": pa_reg,
            }
            if any((pa_operator, pa_model, pa_type, pa_reg))
            else None
        )
        record = {
            "icao": address,
            "address_type": address_type,
            "first": _iso(first_seen),
            "last": _iso(last_seen),
            "band": ",".join(str(int(b) // 1_000_000) for b in sorted(bands or [])),
            "snapshots": snapshots,
            "messages": messages,
            "alt": max_alt,
            "cs": callsign or "",
            # One message and never heard again: probably a decoding error.
            "unconfirmed": (messages or 0) < 2,
        }
        record.update(
            identify(
                address,
                callsign,
                {"registration": faa_reg, "manufacturer": faa_mfr, "model": faa_model, "registered_owner": faa_owner},
                {
                    "registration": os_reg,
                    "model": os_model,
                    "icao_type_code": os_type,
                    "operator": os_operator,
                    "operator_icao": os_operator_icao,
                    "registered_owner": os_owner,
                    "country": os_country,
                },
                types,
                plane_alert,
                operator_code_name,
                tar1090,
            )
        )
        result.append(record)
    return result
