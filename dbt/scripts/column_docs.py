"""Generate models/public/_columns.md (doc blocks) and the column lists in
models/public/_public.yml from one table of column meanings.

Run from dbt/: python scripts/column_docs.py. Edit COLUMNS/OVERRIDES here,
not the generated files. Agents (MCP server, notable agent) read these
descriptions from BigQuery, so state units, time zones and null meaning.
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

import yaml

COLUMNS = {
    # identity
    "address_type": "How the transponder address was assigned: 'icao' for a registered 24-bit ICAO address; 'readsb:*' types are anonymous, TIS-B or non-ICAO and identify no registered aircraft. An aircraft is identified by address_type plus address.",
    "address": "24-bit transponder address as six upper-case hex digits (e.g. ADFDF9). Join key for an aircraft, together with address_type.",
    "registration": "Registration (tail number), e.g. N969NN, from the highest-precedence identity source. Null when no source identifies the aircraft.",
    "designator": "ICAO Doc 8643 type designator, e.g. B738. FAA model text is mapped first, then tar1090-db, OpenSky, plane-alert-db. Null when unknown.",
    "type_name": "Readable aircraft type, e.g. 'Boeing 737-800'. For military aircraft on plane-alert-db, its more specific name. Describes the type, not this airframe.",
    "aircraft_description": "Manufacturer and model as the identity source writes it (e.g. 'BOEING 737-823').",
    "aircraft_class": "One of: Narrow-body / regional jet, Wide-body jet, Business jet, Turboprop, Piston, Helicopter, Other, Unidentified (no type known). Derived from ICAO Doc 8643.",
    "aircraft_kind": "Military (any military rule fired), Airline (airline callsign prefix, airline-like owner or operator, or an airliner owned by a lessor), or Other (general aviation, business, government civil). Null in live data for aircraft not yet classified.",
    "identity_source": "Highest-precedence source identifying the aircraft: faa_registry (official), tar1090_db (community), opensky (crowdsourced, may be stale), plane_alert_mil (military list). Null means unidentified.",
    "owner_name": "Registered owner, shown only when it is an organization (FAA corporation, government, LLC or non-citizen corporation; or an organization name for non-US aircraft). Null for private individuals, trusts and unverified owners: never infer or guess a private owner.",
    "owner_category": "Organization category of a shown owner (e.g. Corporation, Government, LLC, Organization); 'Private or unverified' when the name is withheld; 'Unknown' when no source lists an owner.",
    "operator": "Who operates the aircraft: for military aircraft plane-alert-db's operator or the military operator-code name; otherwise OpenSky's operator, which is crowdsourced and can be stale. Not the owner.",
    "country": "Country of registration according to OpenSky.",
    "year_manufactured": "Year built, from the FAA registry (US aircraft only). Null until imported or for non-US aircraft.",
    "is_military": "True when any military rule fired (see military_rules).",
    "military_rules": "Every rule that classified the aircraft as military: us_military_address_block (ICAO AE0000-AFFFFF), plane_alert_db, tar1090_military_flag, military_operator_code (callsign prefix such as RCH), us_coast_guard_callsign (supporting evidence only). Empty for civil aircraft.",
    "is_miscode": "True when tar1090-db reports the transponder broadcasts an invalid address; the identity is then not meaningful.",
    "latest_callsign": "Most recent flight identification (callsign) broadcast, e.g. SWA2813.",
    "callsigns": "Every distinct callsign broadcast (in this row's scope).",
    "first_seen_at": "First reception of any message (UTC).",
    "last_seen_at": "Latest reception of any message (UTC).",
    "is_confirmed": "True when the address was heard in at least 2 messages over all time: the canonical definition of an aircraft. Single-message addresses are usually decoding errors; filter on this when counting aircraft.",
    "bands_mhz": "Receive bands the aircraft was heard on: 1090 (ADS-B/Mode S) and/or 978 (UAT).",
    "max_altitude_baro_ft": "Highest barometric altitude reported, in feet.",
    "positioned_snapshots": "Snapshots with a decoded position. Many Mode S aircraft report no position.",
    # snapshots
    "snapshot_id": "Surrogate key of the snapshot (bucket, receiver, address type, address, frequency).",
    "bucket_start": "Start of the UTC-aligned 15-second bucket [bucket_start, bucket_start + 15 s). Partition column: always filter on it.",
    "first_received_at": "First message from the aircraft in the bucket (UTC).",
    "last_received_at": "Last message from the aircraft in the bucket (UTC).",
    "message_count": "Valid messages received from the aircraft in the bucket.",
    "callsign": "Flight identification broadcast, trimmed; null if none was received.",
    "latitude": "Latest decoded latitude, degrees. May be carried forward up to 60 s from an earlier bucket (see fields for the reception time).",
    "longitude": "Latest decoded longitude, degrees.",
    "position": "GEOGRAPHY point of longitude/latitude; null without a position.",
    "altitude_baro_ft": "Barometric altitude, feet. Values above 60,000 ft are decoding errors.",
    "altitude_geom_ft": "Geometric (GNSS) altitude, feet.",
    "ground_speed_kt": "Ground speed, knots.",
    "track_deg": "Track over ground, degrees true.",
    "vertical_rate_baro_fpm": "Barometric vertical rate, feet per minute (negative = descending).",
    "vertical_rate_geom_fpm": "Geometric vertical rate, feet per minute.",
    "squawk": "Mode A code, four octal digits. 7500 hijack, 7600 radio failure, 7700 emergency, 1200 US VFR.",
    "on_ground": "True when the aircraft reported it was on the ground.",
    "fields": "Every decoded field as {value, timestamp (Unix seconds of original reception), source}. Carried-forward values keep their original timestamp.",
    "band_mhz": "Receive band, MHz: 1090 or 978.",
    # sightings
    "sighting_id": "Surrogate key of the sighting (address type, address, sighting number).",
    "sighting_number": "Sequence number of the sighting for this aircraft, starting at 1.",
    "ended_at": "Last reception in the sighting (UTC).",
    "squawks": "Every Mode A code squawked during the sighting.",
    "min_altitude_baro_ft": "Lowest barometric altitude reported in the sighting, feet.",
    "max_ground_speed_kt": "Highest ground speed reported in the sighting, knots.",
    "seen_on_ground": "True when the aircraft reported being on the ground during the sighting.",
    "emergency_squawk": "True when the aircraft squawked 7500, 7600 or 7700 during the sighting.",
    "duration_minutes": "Minutes from first to last reception in the sighting.",
    "path_nm": "Distance flown, nautical miles: sum of segments between positions at most 2 minutes apart. Null without positions.",
    "displacement_nm": "Straight-line distance between the sighting's first and last positions, nautical miles.",
    "circling_ratio": "path_nm / displacement_nm, when displacement exceeds 0.5 nm. Near 1 is a straight transit; well above 1 means circling, orbits or pattern work (common for flight schools).",
    "total_turn_deg": "Sum of absolute track changes between snapshots at most 1 minute apart, degrees. 360 is one full turn.",
    "low_away_from_airport_snapshots": "Positioned snapshots below 4,500 ft barometric, airborne, more than 5 nm from every local airport (KLAS, KVGT, KHND, KLSV, KBVU, KINS). Las Vegas terrain is about 2,000-3,000 ft.",
    "closest_airport_nm": "Closest the sighting came to a local airport, nautical miles.",
    # types
    "doc8643_description": "ICAO description code: kind (L land, S sea, A amphibian, H helicopter, G gyrocopter, T tiltrotor), engine count, engine type (J jet, T turboprop, P piston, E electric). E.g. L2J.",
    "wake_category": "ICAO wake turbulence category: L light, M medium, H heavy, J super.",
    "engine_type": "Engine type per Doc 8643 (Jet, Turboprop/Turboshaft, Piston, Electric).",
    "engine_count": "Number of engines per Doc 8643.",
    "aircraft_heard": "Confirmed public aircraft of this type ever heard here. Low values mean the type is rare at this receiver.",
    "military_aircraft_heard": "Confirmed military aircraft of this type ever heard here.",
    "wikipedia_article_url": "English Wikipedia article for the type.",
    "image_url": "Wikimedia Commons image of a representative aircraft of the type (not this airframe).",
    "image_file_url": "Wikimedia Commons file page (credit and license) for image_url.",
    # callwords
    "callword": "Callsign family word, e.g. TALON from TALON95.",
    "lookup_status": "found or not_found.",
    "source_url": "War Watch Intel page the lookup came from.",
    "fetched_at": "When the lookup was fetched (UTC). Cached 30 days if found, 7 if not.",
    "lookup_data": "Aircraft types and countries recorded using the callword. Supporting evidence only: callwords are shared across units.",
    # daily traffic
    "day": "Calendar day in Pacific time (America/Los_Angeles).",
    "confirmed_aircraft": "Distinct confirmed aircraft heard that day on the band.",
    "military_aircraft": "Distinct confirmed military aircraft heard that day on the band.",
    "identified_aircraft": "Distinct confirmed aircraft heard that day that an identity source identifies.",
    "first_seen_aircraft": "Confirmed aircraft heard for the first time ever that day. Inflated before about November 2026: collection began 2026-09-26.",
    "identification_rate": "identified_aircraft / confirmed_aircraft.",
    "messages_per_listening_hour": "Messages per hour actually spent listening on the band. Use this, not raw counts, to compare days: dwell time per band changed on 2026-09-29.",
    "aircraft_per_listening_hour": "Confirmed aircraft per hour spent listening on the band.",
    # uptime
    "hour_start": "Start of the UTC hour.",
    "listening_seconds_1090": "Seconds spent listening on 1090 MHz in the hour.",
    "listening_seconds_978": "Seconds spent listening on 978 MHz in the hour.",
    "uptime_ratio": "Share of the hour spent listening on either band. Below 1.0 even without outages because of retuning between bands; well below means an outage.",
    # data health
    "check_type": "test (a dbt data test) or source (a freshness check that data is still arriving).",
    "check_name": "Name of the test, or source table for freshness checks (e.g. raw.capture_intervals).",
    "latest_status": "Latest result: pass, warn, fail or error.",
    "latest_failures": "Failing rows in the latest run of a test.",
    "latest_age_seconds": "For freshness checks: age of the newest row at the latest check, seconds.",
    "last_checked_at": "When the check last ran (UTC).",
    "last_passed_at": "When the check last passed (UTC); null if not in the last 7 days.",
    "runs_7d": "Scheduled runs of the check in the last 7 days.",
    "failed_runs_7d": "Runs in the last 7 days that failed or errored.",
    "warned_runs_7d": "Runs in the last 7 days that warned.",
    # dbt runs
    "mode": "hourly (build except daily models), daily (build everything) or freshness.",
    "duration_seconds": "Seconds dbt took.",
    "nodes": "Models, tests and sources processed.",
    "errors": "Nodes that failed or errored.",
    "warnings": "Nodes that warned.",
    "bytes_billed": "BigQuery bytes billed by the run.",
    "passed": "True when the run had no errors.",
    # definitions
    "kind": "metric, model (a public view) or column.",
    "name": "Metric, view or column name.",
    "model": "View the column belongs to (null for metrics).",
    "label": "Readable metric name.",
    "description": "Canonical definition.",
    "metric_type": "simple (one measure) or ratio (one metric divided by another).",
    "formula": "Measure (simple metrics) or numerator / denominator metrics (ratios).",
    "filter": "Filter applied to the measure, as a MetricFlow template.",
    "exported_at": "When the definitions were exported from dbt (UTC).",
}

# Same column name, different scope.
OVERRIDES = {
    ("pub_aircraft", "snapshots"): "15-second snapshots of the aircraft over all time.",
    ("pub_aircraft", "messages"): "Messages received from the aircraft over all time.",
    ("pub_sightings", "snapshots"): "15-second snapshots in the sighting.",
    ("pub_sightings", "messages"): "Messages received in the sighting.",
    ("pub_sightings", "started_at"): "First reception in the sighting (UTC).",
    ("pub_sightings", "is_military"): "True when the aircraft is military (any military rule fired).",
    ("pub_sightings", "is_confirmed"): "True when the aircraft is confirmed (2+ messages over all time).",
    ("pub_daily_traffic", "snapshots"): "15-second snapshots recorded that day on the band.",
    ("pub_daily_traffic", "messages"): "Messages received that day on the band.",
    ("pub_daily_traffic", "listening_seconds"): "Seconds spent listening on the band that day (turns started that day, Pacific time).",
    ("pub_daily_traffic", "band_mhz"): "Receive band, MHz: 1090 or 978.",
    ("pub_receiver_uptime_hourly", "listening_seconds"): "Seconds spent listening on either band in the hour.",
    ("pub_dbt_runs", "started_at"): "When the run started (UTC).",
    ("pub_aircraft_types", "first_seen_at"): "First time any public aircraft of the type was heard (UTC).",
    ("pub_aircraft_types", "last_seen_at"): "Latest time any public aircraft of the type was heard (UTC).",
    ("pub_definitions", "description"): "Canonical definition text.",
}


def block_name(model: str, column: str) -> str:
    if (model, column) in OVERRIDES:
        return f"col_{model.removeprefix('pub_')}__{column}"
    return f"col_{column}"


def main(columns_csv: str) -> None:
    rows = list(csv.DictReader(open(columns_csv)))
    missing = sorted({r["column_name"] for r in rows
                      if (r["table_name"], r["column_name"]) not in OVERRIDES
                      and r["column_name"] not in COLUMNS})
    if missing:
        sys.exit(f"undocumented columns: {missing}")

    blocks = dict(sorted({f"col_{c}": d for c, d in COLUMNS.items()}.items()))
    blocks.update({block_name(m, c): d for (m, c), d in OVERRIDES.items()})
    md = ["{# Generated by scripts/column_docs.py. Edit that file, not this one. #}\n"]
    md += [f"{{% docs {name} %}}\n{text}\n{{% enddocs %}}\n" for name, text in sorted(blocks.items())]
    Path("models/public/_columns.md").write_text("\n".join(md))

    path = Path("models/public/_public.yml")
    doc = yaml.safe_load(path.read_text())
    by_model: dict[str, list[str]] = {}
    for r in rows:
        by_model.setdefault(r["table_name"], []).append(r["column_name"])
    for model in doc["models"]:
        cols = by_model.get(model["name"], [])
        model["columns"] = [{"name": c, "description": f"{{{{ doc('{block_name(model['name'], c)}') }}}}"}
                            for c in cols]
    header = "# Column descriptions are generated by scripts/column_docs.py.\n"
    path.write_text(header + yaml.safe_dump(doc, sort_keys=False, width=100, allow_unicode=True))
    print(f"{len(blocks)} doc blocks; {sum(len(v) for v in by_model.values())} public columns documented")


if __name__ == "__main__":
    main(sys.argv[1])
