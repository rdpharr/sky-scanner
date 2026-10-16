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
    "owner_name": "Registered owner, shown only when it is an organization: the FAA classifies the registrant as a corporation, government, LLC or non-citizen corporation, or, for aircraft the FAA does not know, the name reads as an organization. Null for private individuals, co-owners, partnerships, trusts and unverified owners: never infer or guess a private owner.",
    "owner_category": "Organization category of a shown owner (e.g. Corporation, Government, LLC, Organization); 'Private or unverified' when the name is withheld; 'Unknown' when no source lists an owner.",
    "operator": "Who operates the aircraft: for military aircraft plane-alert-db's operator or the military operator-code name; otherwise OpenSky's operator, which is crowdsourced and can be stale. Not the owner.",
    "country": "Country of registration according to OpenSky.",
    "year_manufactured": "Year built, from the FAA registry (US aircraft only). Null until imported or for non-US aircraft.",
    "is_military": "True when any military rule fired (see military_rules).",
    "military_rules": "Every rule that classified the aircraft as military: us_military_address_block (ICAO AE0000-AFFFFF), plane_alert_db, tar1090_military_flag, military_operator_code (callsign prefix such as RCH), us_coast_guard_callsign (supporting evidence only). Empty for civil aircraft.",
    "is_ladd": "True when the aircraft is on the FAA LADD list: its owner asked the FAA to limit display of its flights on FAA-derived tracking services. Included here because it is received directly over the air; say so when you mention it.",
    "is_pia": "True when the address is an FAA Privacy ICAO Address, a temporary address meant to hide the registration. Identity data for it is unreliable; say so when you mention it.",
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
    "wikipedia_title": "Title of the type's English Wikipedia article.",
    "wikipedia_description": "Wikipedia's one-line description of the type (e.g. 'airborne command post'). Use it, and cite it, for what an aircraft type is.",
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
    "passed": "pub_dbt_runs: true when the run had no errors. pub_eval_runs: number of questions answered correctly or reference items found.",
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
    # eval runs
    "run_id": "Unique id of the evaluation run.",
    "score": "Share passed: Q&A answers matching the gold SQL, or reference items an edition covered.",
    "total": "Questions asked or reference items checked.",
    "details": "Per question (answer, gold value, pass, tool calls, cost) or per reference item (found or missing).",
    # notable editions
    "edition_id": "Unique id of the generated edition.",
    "week_start": "First Pacific day the edition covers.",
    "week_end": "Day after the last Pacific day the edition covers (exclusive).",
    "generated_at": "When the agent wrote the edition (UTC).",
    "purpose": "weekly (published on the website), eval (written to measure the agent) or test.",
    "status": "published, rejected (too few statements verified), eval or test.",
    "model": "OpenRouter model that wrote the edition.",
    "edition": "The verified edition: title, summary, sections with entries, privacy and caveats. Every statement carries the evidence (tool call, path, value) it was verified against.",
    "photos": "Wikipedia/Wikimedia Commons photo per type designator, with author and license.",
    "statements_checked": "Statements in the agent's draft.",
    "statements_verified": "Statements whose every cited value matched the tool output and appeared in the sentence.",
    "verification_rate": "statements_verified / statements_checked.",
    "removed_statements": "Statements the verifier removed, with where they were and why. Shown for transparency; their content is unverified.",
    "tool_calls": "The agent's tool calls in order: call id, tool, arguments and whether it succeeded.",
    "tool_call_count": "Number of tool calls the agent made.",
    "turns": "Model turns in the agent loop.",
    "prompt_tokens": "Prompt tokens billed by OpenRouter.",
    "completion_tokens": "Completion tokens billed by OpenRouter.",
    "cost_usd": "OpenRouter cost of the edition, US dollars.",
    "seconds": "Wall-clock seconds the agent took.",
    # notable candidates
    "window_start": "First Pacific day of the 7-day window (inclusive).",
    "window_end": "Day after the window's last Pacific day (exclusive). An edition published on a Monday covers window_end = that Monday.",
    "sightings_7d": "Sightings in the last 7 days.",
    "days_seen_7d": "Pacific-time days with at least one sighting in the last 7 days.",
    "messages_7d": "Messages received in the last 7 days.",
    "max_circling_ratio_7d": "Highest circling_ratio of any sighting in the last 7 days.",
    "low_away_snapshots_7d": "Snapshots in the last 7 days below 4,500 ft, airborne, more than 5 nm from local airports.",
    "emergency_code_7d": "True when the aircraft squawked 7500, 7600 or 7700 in the last 7 days.",
    "max_altitude_baro_ft_7d": "Highest barometric altitude in the last 7 days, feet.",
    "min_altitude_baro_ft_7d": "Lowest barometric altitude in the last 7 days, feet.",
    "max_ground_speed_kt_7d": "Highest ground speed in the last 7 days, knots.",
    "sightings_prior_28d": "Sightings in the 28 days before the last 7: the aircraft's own baseline.",
    "type_aircraft_heard": "Confirmed public aircraft of this type ever heard here (rarity).",
    "type_first_seen_this_week": "True when no public aircraft of this type was heard before the last 7 days.",
    "notable_score": "Ranking score: 1.0 emergency + 0.45 type rarity + 0.20 military + 0.15 vintage civil + 0.20 civil government/public safety + 0.25 unusual path + 0.15 frequency novelty + 0.10 foreign non-airline, x0.3 for airliners without an emergency. Ranks candidates; not a finding.",
    "vintage_score": "1.0 for a civil, non-airline aircraft the FAA lists as built in 1975 or earlier.",
    "type_score": "Type rarity component, 0-1: 1.0 when this is the only aircraft of its type heard here, plus 0.3 when the type is new this week.",
    "military_score": "1.0 when the aircraft is military.",
    "ownership_score": "1.0 when a civil aircraft is owned or operated by a government or public-safety organization (police, sheriff, federal agency, university).",
    "foreign_score": "1.0 for a foreign-registered aircraft that is not an airliner.",
    "path_score": "Circling or low flying relative to the aircraft's own previous 28 days, 0-1. Low for aircraft that do this routinely (flight schools). Null without positions.",
    "frequency_score": "First-ever, returning or spiking visits, 0-1. Null until about 2026-11-21, when there is enough history.",
    "emergency_score": "1.0 when the aircraft squawked an emergency code in the last 7 days.",
    "reasons": "Readable explanations of every signal that contributed to notable_score.",
    "rank_within_type": "Rank among this week's candidates of the same type (1 = highest score).",
    "candidates_of_type": "Number of this week's candidates of the same type.",
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
    ("pub_eval_runs", "started_at"): "When the evaluation ran (UTC).",
    ("pub_eval_runs", "kind"): "qa (factual questions graded against gold SQL) or edition_recall (reference items an edition covered).",
    ("pub_eval_runs", "model"): "OpenRouter model evaluated.",
    ("pub_eval_runs", "cost_usd"): "OpenRouter cost of the run (for edition_recall, of the edition evaluated), US dollars.",
    ("pub_eval_runs", "seconds"): "Wall-clock seconds the run took.",
    ("pub_hourly_traffic", "hour_start"): "Start of the UTC hour. Every hour since collection began has a row, including hours the receiver was off.",
    ("pub_hourly_traffic", "day_pt"): "Pacific (America/Los_Angeles) date of the hour.",
    ("pub_hourly_traffic", "hour_pt"): "Pacific hour of day, 0-23.",
    ("pub_hourly_traffic", "weekday_pt"): "Pacific day of the week, e.g. 'Monday'.",
    ("pub_hourly_traffic", "listening_seconds"): "Seconds spent listening on either band in the hour.",
    ("pub_hourly_traffic", "uptime_ratio"): "Share of the hour spent listening on either band. About 0.98 when running normally (retuning between bands); below 0.8 means an outage, so counts for that hour understate traffic.",
    ("pub_hourly_traffic", "confirmed_aircraft"): "Distinct confirmed aircraft heard in the hour, either band.",
    ("pub_hourly_traffic", "airline_aircraft"): "Distinct confirmed aircraft of kind Airline heard in the hour.",
    ("pub_hourly_traffic", "military_aircraft"): "Distinct confirmed aircraft of kind Military heard in the hour.",
    ("pub_hourly_traffic", "other_aircraft"): "Distinct confirmed aircraft of kind Other (general aviation, business, government civil) heard in the hour.",
    ("pub_hourly_traffic", "sightings_started"): "Sightings of confirmed aircraft that started in the hour.",
    ("pub_hourly_traffic", "messages"): "Messages received in the hour from confirmed aircraft, either band.",
    ("pub_hourly_traffic", "aircraft_per_listening_hour"): "confirmed_aircraft divided by listening hours: comparable across hours with different uptime. Null when the receiver did not listen.",
    ("pub_daily_mix", "day"): "Calendar day in Pacific time (America/Los_Angeles).",
    ("pub_daily_mix", "aircraft_class"): "Aircraft class (see pub_aircraft.aircraft_class).",
    ("pub_daily_mix", "aircraft_kind"): "Military, Airline or Other (see pub_aircraft.aircraft_kind).",
    ("pub_daily_mix", "identity_source"): "Highest-precedence source identifying the aircraft (faa_registry, tar1090_db, opensky, plane_alert_mil), or 'Unidentified'.",
    ("pub_daily_mix", "confirmed_aircraft"): "Distinct confirmed aircraft heard that day with this class, kind and source. Each aircraft falls in one row per day, so sums across rows are distinct counts.",
    ("pub_daily_mix", "sightings"): "Sightings that started that day by those aircraft.",
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
        if model["name"] not in by_model:
            continue  # not in the input: keep its existing column docs
        model["columns"] = [{"name": c, "description": f"{{{{ doc('{block_name(model['name'], c)}') }}}}"}
                            for c in by_model[model["name"]]]
    header = "# Column descriptions are generated by scripts/column_docs.py.\n"
    path.write_text(header + yaml.safe_dump(doc, sort_keys=False, width=100, allow_unicode=True))
    print(f"{len(blocks)} doc blocks; {sum(len(v) for v in by_model.values())} public columns documented")


if __name__ == "__main__":
    main(sys.argv[1])
