"""Sky Scanner MCP server: read-only tools over the BigQuery `public` dataset.

Runs over Streamable HTTP on Cloud Run (MCP_TRANSPORT=http) or stdio for a
local client (the default). It authenticates to BigQuery as sky-mcp, which can
read only the public views: no tool can reach private owners' names or raw
tables, whatever it is asked. Every query is parameterized and capped
in bytes billed.
"""

from __future__ import annotations

import hmac
import os
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from google.api_core.exceptions import GoogleAPICallError
from google.cloud import bigquery
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

PROJECT = os.environ.get("GCP_PROJECT", "sky-scanner-pharr")
DATASET = os.environ.get("PUBLIC_DATASET", "public")
MAX_BYTES_BILLED = int(os.environ.get("MAX_BYTES_BILLED", 300_000_000))
SQL_MAX_BYTES = int(os.environ.get("SQL_MAX_BYTES", 1_000_000_000))
SQL_TOKEN = os.environ.get("MCP_SQL_TOKEN", "")
CALLS_PER_MINUTE = int(os.environ.get("MCP_CALLS_PER_MINUTE", 120))
MAX_ROWS = 200

INSTRUCTIONS = """\
Data from a receive-only ADS-B (1090 MHz) and UAT (978 MHz) station near Las
Vegas, Nevada, collecting since 2026-09-26. Aircraft broadcast their identity,
position, altitude and speed; the station stores one snapshot per aircraft per
15 seconds and identifies aircraft from the FAA registry, tar1090-db, OpenSky
and plane-alert-db. Data arrives within about a minute; derived tables
(aircraft, sightings, daily traffic) rebuild hourly.

Definitions are canonical and tested; call get_definitions before computing a
metric yourself. Key ones: an aircraft is "confirmed" when heard in 2+
messages ever (single-message addresses are decoding errors); a "sighting" is a
run of reports with no gap over 10 minutes; "military" means at least one of
the rules in military_rules fired.

Rules for answers:
- Times are UTC unless a column says Pacific; say which you use.
- Cite where each fact came from (tool and field). Identity sources differ in
  reliability: FAA is official; OpenSky is crowdsourced and can be stale.
- Owner names appear only for organizations. Never guess or infer who owns a
  privately registered aircraft or who is aboard any aircraft.
- Aircraft on the FAA LADD list (is_ladd) are included because they are heard
  directly; say when an aircraft is LADD-listed. Privacy ICAO Address aircraft
  (is_pia) have unreliable identities; say so.
- Coverage is partial: many aircraft report no position, the radio splits time
  between two bands, and outages happen (see receiver_status).
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

mcp = MCPServer(
    "sky-scanner",
    title="Sky Scanner",
    description="Aircraft heard by a home ADS-B/UAT receiver near Las Vegas.",
    instructions=INSTRUCTIONS,
    website_url="https://skyscanner.pharrhomelab.com",
    version="1.0.0",
)

_client: bigquery.Client | None = None
_calls: deque[float] = deque()
_calls_lock = threading.Lock()


def client() -> bigquery.Client:
    global _client
    if _client is None:
        _client = bigquery.Client(project=PROJECT)
    return _client


def table(name: str) -> str:
    return f"`{PROJECT}.{DATASET}.{name}`"


def rate_limit() -> None:
    """Simple per-instance limit on tool calls, to bound query costs."""
    now = time.monotonic()
    with _calls_lock:
        while _calls and now - _calls[0] > 60:
            _calls.popleft()
        if len(_calls) >= CALLS_PER_MINUTE:
            raise ToolError("Rate limit reached; try again in a minute.")
        _calls.append(now)


# Coordinates keep their precision; other measurements are rounded so agents
# quote readable numbers (27000 ft, 455 kt, 8.7 nm) rather than 454.881866.
EXACT = {"latitude", "longitude", "south", "north", "east", "west"}


def jsonable(value: Any, key: str = "") -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, float) and key not in EXACT:
        return int(round(value)) if abs(value) >= 100 or value.is_integer() else round(value, 1)
    if isinstance(value, list):
        return [jsonable(v, key) for v in value]
    if isinstance(value, dict):
        return {k: jsonable(v, k) for k, v in value.items()}
    return value


def _param(name: str, value: Any) -> bigquery.ScalarQueryParameter:
    if isinstance(value, bool):
        kind = "BOOL"
    elif isinstance(value, int):
        kind = "INT64"
    elif isinstance(value, float):
        kind = "FLOAT64"
    else:
        kind = "STRING"
    return bigquery.ScalarQueryParameter(name, kind, value)


def query(sql: str, **params: Any) -> list[dict[str, Any]]:
    rate_limit()
    config = bigquery.QueryJobConfig(
        maximum_bytes_billed=MAX_BYTES_BILLED,
        query_parameters=[_param(k, v) for k, v in params.items()],
        labels={"component": "mcp"},
    )
    rows = client().query(sql, job_config=config).result(max_results=MAX_ROWS)
    return [jsonable(dict(row.items())) for row in rows]


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


PACIFIC = ZoneInfo("America/Los_Angeles")


def date_range(start_date: str, end_date: str) -> tuple[str, str]:
    """Pacific dates, both inclusive ("26 September to 2 October" is
    start_date 2026-09-26, end_date 2026-10-02). Returns (start, day after
    end) for SQL. Defaults: the 7 complete days before today. Inclusive
    because an exclusive end date silently dropped the last day for agents
    in evaluation (evals/qa_questions.json)."""
    try:
        last = date.fromisoformat(end_date) if end_date else datetime.now(PACIFIC).date() - timedelta(days=1)
        start = date.fromisoformat(start_date) if start_date else last - timedelta(days=6)
    except ValueError:
        raise ToolError("Dates must be YYYY-MM-DD (Pacific).") from None
    if not timedelta(days=0) <= last - start <= timedelta(days=91):
        raise ToolError("end_date must be on or after start_date, at most 92 days.")
    return start.isoformat(), (last + timedelta(days=1)).isoformat()


# SQL for "this timestamp falls in the Pacific date range [@start, @end)".
IN_RANGE = "{col} >= timestamp(@start, 'America/Los_Angeles') and {col} < timestamp(@end, 'America/Los_Angeles')"


def resolve(identifier: str) -> dict[str, Any]:
    """Find one public aircraft by address, registration or callsign."""
    rows = query(
        f"""
        select address_type, address
        from {table("pub_aircraft")}
        where address = @id or upper(registration) = @id or latest_callsign = @id
           or @id in unnest(callsigns)
        order by (address = @id) desc, (upper(registration) = @id) desc, last_seen_at desc
        limit 1""",
        id=identifier.strip().upper(),
    )
    if not rows:
        raise ToolError(f"No aircraft matches {identifier!r}. Try search_aircraft.")
    return rows[0]


# ---------------------------------------------------------------- context


@mcp.tool(annotations=READ_ONLY)
def get_definitions(term: str = "") -> dict[str, Any]:
    """Canonical definitions of metrics and public columns, exported from dbt.

    With no term: every metric plus a one-line description of each public view.
    With a term (e.g. "sighting", "confirmed", "circling"): metrics and columns
    whose name or definition mentions it. Prefer these definitions to your own.
    """
    rows = query(
        f"""
        select kind, name, model, label, description, metric_type, formula, filter
        from {table("pub_definitions")}
        where kind = 'metric'
           or (@term = '' and kind = 'model')
           or (@term != '' and (lower(name) like concat('%', @term, '%')
                                or lower(description) like concat('%', @term, '%')))
        order by kind, model, name""",
        term=term.strip().lower(),
    )
    metrics = [
        r
        for r in rows
        if r["kind"] == "metric" and (not term or term.lower() in (r["name"] + (r["description"] or "")).lower())
    ]
    if not term:
        views = [{"view": r["name"], "description": r["description"]} for r in rows if r["kind"] == "model"]
        return {"metrics": metrics, "views": views}
    columns = [
        {"view": r["model"], "column": r["name"], "description": r["description"]}
        for r in rows
        if r["kind"] == "column"
    ]
    return {"metrics": metrics, "columns": columns}


@mcp.tool(annotations=READ_ONLY)
def describe_dataset(view: str = "") -> dict[str, Any]:
    """The public BigQuery views this server reads, with every column's meaning.

    Give a view name (e.g. "pub_sightings") for its columns, or nothing for
    the list of views. Useful before run_sql.
    """
    rows = query(
        f"""
        select kind, name, model, description from {table("pub_definitions")}
        where (@view = '' and kind = 'model') or (@view != '' and model = @view)
        order by model, kind desc""",
        view=view.strip(),
    )
    views: dict[str, dict[str, Any]] = {}
    for r in rows:
        entry = views.setdefault(r["model"], {"view": r["model"], "description": None, "columns": {}})
        if r["kind"] == "model":
            entry["description"] = r["description"]
        elif view:
            entry["columns"][r["name"]] = r["description"]
    return {"dataset": f"{PROJECT}.{DATASET}", "views": list(views.values())}


# ---------------------------------------------------------------- aircraft

AIRCRAFT_COLUMNS = """address_type, address, registration, designator, type_name, aircraft_class,
    aircraft_kind, identity_source, owner_name, owner_category, operator, country,
    year_manufactured, is_military, military_rules, latest_callsign, callsigns,
    first_seen_at, last_seen_at, snapshots, messages, bands_mhz, max_altitude_baro_ft"""


@mcp.tool(annotations=READ_ONLY)
def search_aircraft(
    text: str = "", kind: str = "", military_only: bool = False, heard_within_days: int = 30, limit: int = 20
) -> list[dict[str, Any]]:
    """Find confirmed aircraft by registration, callsign, type, operator or address.

    kind: "Airline", "Military" or "Other" (empty for all). heard_within_days
    filters on last_seen_at (0 for all time). Newest first, at most 50.
    """
    return query(
        f"""
        select {AIRCRAFT_COLUMNS} from {table("pub_aircraft")}
        where is_confirmed
          and (@text = '' or upper(concat(ifnull(registration,''), ' ', ifnull(latest_callsign,''), ' ',
               ifnull(type_name,''), ' ', ifnull(operator,''), ' ', address, ' ', ifnull(designator,'')))
               like concat('%', @text, '%'))
          and (@kind = '' or aircraft_kind = @kind)
          and (not @military_only or is_military)
          and (@days = 0 or last_seen_at >= timestamp_sub(current_timestamp(), interval @days day))
        order by last_seen_at desc limit @limit""",
        text=text.strip().upper(),
        kind=kind.strip().capitalize(),
        military_only=military_only,
        days=clamp(heard_within_days, 0, 3650),
        limit=clamp(limit, 1, 50),
    )


@mcp.tool(annotations=READ_ONLY)
def get_aircraft(identifier: str) -> dict[str, Any]:
    """Everything known about one aircraft: identity (with its source),
    classification, military evidence, lifetime activity and recent sightings.

    identifier: ICAO address (ADFDF9), registration (N969NN) or callsign (SWA2813).
    """
    key = resolve(identifier)
    aircraft = query(
        f"""select {AIRCRAFT_COLUMNS}, is_confirmed from {table("pub_aircraft")}
                         where address_type = @t and address = @a""",
        t=key["address_type"],
        a=key["address"],
    )[0]
    aircraft["recent_sightings"] = query(
        f"""
        select started_at, ended_at, duration_minutes, messages, callsigns, min_altitude_baro_ft,
               max_altitude_baro_ft, max_ground_speed_kt, positioned_snapshots, circling_ratio,
               emergency_squawk
        from {table("pub_sightings")} where address_type = @t and address = @a
        order by started_at desc limit 5""",
        t=key["address_type"],
        a=key["address"],
    )
    if aircraft["is_military"] and aircraft["callsigns"]:
        # War Watch Intel callsign families (e.g. TALON for TALON95): supporting
        # evidence only, since callwords are shared across units.
        lookups = query(f"""select callword, lookup_status, source_url, lookup_data
                            from {table("pub_callword_lookups")}""")
        aircraft["callword_lookups"] = [
            r for r in lookups if any(cs.startswith(r["callword"]) for cs in aircraft["callsigns"])
        ]
    return aircraft


@mcp.tool(annotations=READ_ONLY)
def get_sightings(
    identifier: str, days: int = 30, limit: int = 50, start_date: str = "", end_date: str = ""
) -> list[dict[str, Any]]:
    """An aircraft's sightings (runs of reports with no gap over 10 minutes),
    newest first, with altitude, speed and flight-path features. Give
    start_date/end_date (Pacific YYYY-MM-DD, inclusive) for a specific
    period; otherwise the last `days` days."""
    key = resolve(identifier)
    if start_date or end_date:
        start, end = date_range(start_date, end_date)
        return query(
            f"""
            select sighting_number, started_at, ended_at, duration_minutes, snapshots, messages,
                   bands_mhz, callsigns, squawks, min_altitude_baro_ft, max_altitude_baro_ft,
                   max_ground_speed_kt, positioned_snapshots, path_nm, displacement_nm, circling_ratio,
                   total_turn_deg, low_away_from_airport_snapshots, closest_airport_nm, emergency_squawk
            from {table("pub_sightings")}
            where address_type = @t and address = @a and {IN_RANGE.format(col="started_at")}
            order by started_at desc limit @limit""",
            t=key["address_type"],
            a=key["address"],
            start=start,
            end=end,
            limit=clamp(limit, 1, 200),
        )
    return query(
        f"""
        select sighting_number, started_at, ended_at, duration_minutes, snapshots, messages,
               bands_mhz, callsigns, squawks, min_altitude_baro_ft, max_altitude_baro_ft,
               max_ground_speed_kt, positioned_snapshots, path_nm, displacement_nm, circling_ratio,
               total_turn_deg, low_away_from_airport_snapshots, closest_airport_nm, emergency_squawk
        from {table("pub_sightings")}
        where address_type = @t and address = @a
          and started_at >= timestamp_sub(current_timestamp(), interval @days day)
        order by started_at desc limit @limit""",
        t=key["address_type"],
        a=key["address"],
        days=clamp(days, 1, 3650),
        limit=clamp(limit, 1, 200),
    )


@mcp.tool(annotations=READ_ONLY)
def get_flight_summary(identifier: str, sighting_started_at: str = "") -> dict[str, Any]:
    """Describe one sighting's flight: duration, altitude band, speed, distance
    flown, straight-line displacement, circling and low flying away from
    airports. Defaults to the latest sighting.

    sighting_started_at: started_at of a sighting from get_sightings (ISO UTC).
    Interpretation: circling_ratio near 1 is a transit; above about 2 is
    circling or pattern work (common for flight schools and police). Path
    features are null when no positions were decoded.
    """
    key = resolve(identifier)
    rows = query(
        f"""
        select * except (address_type, address, sighting_id)
        from {table("pub_sightings")}
        where address_type = @t and address = @a
          and (@start = '' or started_at = timestamp(@start))
        order by started_at desc limit 1""",
        t=key["address_type"],
        a=key["address"],
        start=sighting_started_at.strip(),
    )
    if not rows:
        raise ToolError("No such sighting; list them with get_sightings.")
    sighting = rows[0]
    stats = query(
        f"""
        select count(*) as snapshots,
               round(avg(altitude_baro_ft)) as avg_altitude_baro_ft,
               round(avg(ground_speed_kt)) as avg_ground_speed_kt,
               round(min(latitude), 2) as south, round(max(latitude), 2) as north,
               round(min(longitude), 2) as west, round(max(longitude), 2) as east
        from {table("pub_snapshots")}
        where address_type = @t and address = @a
          and bucket_start between timestamp_sub(timestamp(@start), interval 15 second)
                               and timestamp(@end)""",
        t=key["address_type"],
        a=key["address"],
        start=sighting["started_at"],
        end=sighting["ended_at"],
    )
    return {"address": key["address"], "sighting": sighting, "track": stats[0] if stats else None}


@mcp.tool(annotations=READ_ONLY)
def get_type_info(designator: str) -> dict[str, Any]:
    """An aircraft type (ICAO designator, e.g. B738, H60, VC25): class, engines,
    how many public aircraft of it were heard and how rare it is here, with
    example aircraft and a Wikipedia article."""
    rows = query(
        f"""
        with ranked as (
            select *, rank() over (order by aircraft_heard desc) as popularity_rank,
                   count(*) over () as types_heard
            from {table("pub_aircraft_types")})
        select * from ranked where designator = @d""",
        d=designator.strip().upper(),
    )
    if not rows:
        raise ToolError(f"No public aircraft of type {designator!r} has been heard.")
    info = rows[0]
    info["examples"] = query(
        f"""
        select registration, latest_callsign, operator, aircraft_kind, last_seen_at
        from {table("pub_aircraft")} where designator = @d and is_confirmed
        order by last_seen_at desc limit 5""",
        d=info["designator"],
    )
    return info


@mcp.tool(annotations=READ_ONLY)
def get_history(identifier: str) -> dict[str, Any]:
    """How often an aircraft is heard: sightings per week over all time, days
    seen, and the hours of day (Pacific) it is usually heard. Use it to tell a
    regular from a first-time or unusual visitor."""
    key = resolve(identifier)
    weekly = query(
        f"""
        select date_trunc(date(started_at, 'America/Los_Angeles'), week(monday)) as week_starting,
               count(*) as sightings, sum(messages) as messages
        from {table("pub_sightings")} where address_type = @t and address = @a
        group by week_starting order by week_starting""",
        t=key["address_type"],
        a=key["address"],
    )
    hours = query(
        f"""
        select extract(hour from started_at at time zone 'America/Los_Angeles') as hour_pt,
               count(*) as sightings
        from {table("pub_sightings")} where address_type = @t and address = @a
        group by hour_pt order by hour_pt""",
        t=key["address_type"],
        a=key["address"],
    )
    days = query(
        f"""
        select count(distinct date(started_at, 'America/Los_Angeles')) as days_seen,
               min(started_at) as first_sighting, max(started_at) as latest_sighting
        from {table("pub_sightings")} where address_type = @t and address = @a""",
        t=key["address_type"],
        a=key["address"],
    )[0]
    return {
        "address": key["address"],
        **days,
        "weekly": weekly,
        "hours_pt": hours,
        "note": "Collection began 2026-09-26, so 'first seen' before late November "
        "2026 does not mean the aircraft is new to the area.",
    }


# ---------------------------------------------------------------- activity


@mcp.tool(annotations=READ_ONLY)
def recent_activity(minutes: int = 10) -> dict[str, Any]:
    """Aircraft heard in the last N minutes (1-30), seconds-fresh: latest
    position, altitude and speed per aircraft. Identity fields are null for
    aircraft first heard since the last hourly build."""
    minutes = clamp(minutes, 1, 30)
    aircraft = query(
        f"""
        select address, any_value(callsign having max bucket_start) as callsign,
               any_value(registration) as registration, any_value(type_name) as type_name,
               any_value(aircraft_kind) as aircraft_kind, any_value(operator) as operator,
               max(last_received_at) as last_heard,
               any_value(altitude_baro_ft having max bucket_start) as altitude_baro_ft,
               any_value(ground_speed_kt having max bucket_start) as ground_speed_kt,
               any_value(latitude having max bucket_start) as latitude,
               any_value(longitude having max bucket_start) as longitude,
               sum(message_count) as messages
        from {table("pub_live_snapshots")}
        where bucket_start >= timestamp_sub(current_timestamp(), interval @m minute)
        group by address order by last_heard desc""",
        m=minutes,
    )
    return {"minutes": minutes, "aircraft_count": len(aircraft), "aircraft": aircraft}


@mcp.tool(annotations=READ_ONLY)
def traffic_stats(start_date: str = "", end_date: str = "") -> list[dict[str, Any]]:
    """Daily traffic per band (Pacific days, inclusive; default the last 7
    complete days): confirmed, military and identified aircraft, messages, and
    rates per listening hour. Compare days with the per-listening-hour rates;
    raw counts depend on time spent on each band."""
    start, end = date_range(start_date, end_date)
    return query(
        f"""
        select * from {table("pub_daily_traffic")}
        where day >= date(@start) and day < date(@end)
        order by day desc, band_mhz""",
        start=start,
        end=end,
    )


@mcp.tool(annotations=READ_ONLY)
def military_sightings(start_date: str = "", end_date: str = "") -> list[dict[str, Any]]:
    """Military aircraft heard in a period (Pacific dates, inclusive;
    default the last 7 complete days), with type, operator, the rules that
    classified them as military and their sightings."""
    start, end = date_range(start_date, end_date)
    return query(
        f"""
        with recent as (
            select address_type, address, count(*) as sightings, min(started_at) as first_sighting,
                   max(ended_at) as latest_sighting, max(max_altitude_baro_ft) as max_altitude_baro_ft
            from {table("pub_sightings")}
            where is_military and {IN_RANGE.format(col="started_at")}
            group by address_type, address)
        select a.address, a.registration, a.type_name, a.designator, a.operator, a.military_rules,
               a.callsigns, a.is_ladd, a.is_pia, r.sightings, r.first_sighting, r.latest_sighting,
               r.max_altitude_baro_ft
        from recent r join {table("pub_aircraft")} a using (address_type, address)
        where a.is_confirmed
        order by r.latest_sighting desc""",
        start=start,
        end=end,
    )


@mcp.tool(annotations=READ_ONLY)
def get_notable_candidates(end_date: str = "", limit: int = 25, per_type: int = 1, kind: str = "") -> dict[str, Any]:
    """Aircraft heard in a 7-day window, ranked by a notability score, with
    the reasons each scored. A starting point for "what was notable this
    week": investigate candidates with get_aircraft, get_history,
    get_flight_summary and get_type_info before concluding anything.

    end_date: the window's last Pacific day (inclusive; the window is that
    day and the 6 before it); default the latest window. per_type caps how many
    aircraft of one type are listed (the rest are counted in
    candidates_of_type), so six similar transports do not crowd out others.
    kind: "military" or "civil" lists one side only (empty for both); ask for
    each separately so rare civil aircraft are not crowded out.
    """
    week_ending = date_range("", end_date)[1] if end_date else ""
    rows = query(
        f"""
        select * from {table("pub_notable_candidates")}
        where window_end = coalesce(date(nullif(@week_ending, '')),
                                    (select max(window_end) from {table("pub_notable_candidates")}))
          and rank_within_type <= @per_type
          and (@kind = '' or (@kind = 'military') = is_military)
        order by notable_score desc limit @limit""",
        week_ending=week_ending,
        per_type=clamp(per_type, 1, 10),
        limit=clamp(limit, 1, 50),
        kind=kind.strip().lower() if kind.strip().lower() in ("military", "civil") else "",
    )
    return {
        "scoring": "notable_score = 1.0 emergency squawk + 0.45 type rarity here + 0.20 military "
        "+ 0.15 vintage civil (built 1975 or earlier) "
        "+ 0.20 civil government/public-safety owner or operator + 0.25 flight path "
        "unusual for this aircraft + 0.15 frequency novelty + 0.10 foreign non-airline; "
        "airliners x0.3 unless an emergency. frequency_score is null until about "
        "2026-11-21 (too little history). Scores rank candidates; they are not findings.",
        "candidates": rows,
    }


@mcp.tool(annotations=READ_ONLY)
def week_summary(start_date: str = "", end_date: str = "") -> dict[str, Any]:
    """Totals for a period (Pacific dates, inclusive; default the last 7
    complete days): confirmed aircraft, sightings, snapshots and messages,
    first and last report, aircraft by kind and class, LADD and PIA counts,
    foreign-registered wide-bodies, and listening hours per band."""
    start, end = date_range(start_date, end_date)
    totals = query(
        f"""
        select count(*) as snapshots, sum(message_count) as messages,
               count(distinct address) as addresses_heard,
               min(first_received_at) as first_report, max(last_received_at) as last_report
        from {table("pub_snapshots")} where {IN_RANGE.format(col="bucket_start")}""",
        start=start,
        end=end,
    )[0]
    aircraft = query(
        f"""
        with heard as (
            select distinct address_type, address from {table("pub_sightings")}
            where {IN_RANGE.format(col="started_at")})
        select count(*) as confirmed_aircraft,
               countif(a.aircraft_kind = 'Airline') as airline,
               countif(a.aircraft_kind = 'Military') as military,
               countif(a.aircraft_kind = 'Other') as other,
               countif(a.is_ladd) as ladd_listed, countif(a.is_pia) as privacy_addresses,
               countif(a.aircraft_class = 'Wide-body jet' and a.country != 'United States'
                       and a.aircraft_kind != 'Military') as foreign_wide_bodies,
               countif(a.identity_source is null) as unidentified
        from heard join {table("pub_aircraft")} a using (address_type, address)
        where a.is_confirmed""",
        start=start,
        end=end,
    )[0]
    classes = query(
        f"""
        with heard as (
            select distinct address_type, address from {table("pub_sightings")}
            where {IN_RANGE.format(col="started_at")})
        select a.aircraft_class, count(*) as aircraft
        from heard join {table("pub_aircraft")} a using (address_type, address)
        where a.is_confirmed group by 1 order by 2 desc""",
        start=start,
        end=end,
    )
    sightings = query(
        f"""
        select count(*) as sightings from {table("pub_sightings")}
        where is_confirmed and {IN_RANGE.format(col="started_at")}""",
        start=start,
        end=end,
    )[0]
    listening = query(
        f"""
        select round(sum(listening_seconds_1090) / 3600, 1) as hours_1090,
               round(sum(listening_seconds_978) / 3600, 1) as hours_978
        from {table("pub_receiver_uptime_hourly")} where {IN_RANGE.format(col="hour_start")}""",
        start=start,
        end=end,
    )[0]
    return {
        "start_date": start,
        "end_date": (date.fromisoformat(end) - timedelta(days=1)).isoformat(),
        "times": "UTC timestamps",
        **totals,
        **aircraft,
        **sightings,
        "by_class": classes,
        "listening_hours": listening,
    }


@mcp.tool(annotations=READ_ONLY)
def callsign_activity(prefix: str, start_date: str = "", end_date: str = "") -> dict[str, Any]:
    """Every sighting of aircraft whose callsign starts with a prefix (e.g.
    JANET, RCH, TALON, a company's ICAO code) in a period (Pacific dates,
    inclusive): when (Pacific day, weekday and hour), how high, which way
    (median track and whether climbing or descending). Use it to describe
    schedules and routes, e.g. a shuttle's daily pattern."""
    start, end = date_range(start_date, end_date)
    rows = query(
        f"""
        with s as (
            select * from {table("pub_sightings")}
            where {IN_RANGE.format(col="started_at")}
              and exists(select 1 from unnest(callsigns) c where starts_with(c, @prefix))),
        tracks as (
            select p.address, s.sighting_number,
                   approx_quantiles(p.track_deg, 2 ignore nulls)[safe_offset(1)] as median_track_deg,
                   array_agg(p.altitude_baro_ft ignore nulls order by p.bucket_start limit 1)[safe_offset(0)]
                       as first_alt,
                   array_agg(p.altitude_baro_ft ignore nulls order by p.bucket_start desc limit 1)[safe_offset(0)]
                       as last_alt
            from s join {table("pub_snapshots")} p
              on p.address_type = s.address_type and p.address = s.address
             and p.bucket_start between timestamp_sub(s.started_at, interval 15 second) and s.ended_at
            where {IN_RANGE.format(col="p.bucket_start")}
            group by p.address, s.sighting_number)
        select a.registration, s.address, s.callsigns, a.type_name, a.owner_name,
               format_timestamp('%F', s.started_at, 'America/Los_Angeles') as day_pt,
               format_timestamp('%A', s.started_at, 'America/Los_Angeles') as weekday,
               format_timestamp('%H:%M', s.started_at, 'America/Los_Angeles') as time_pt,
               round(s.duration_minutes, 1) as minutes, s.min_altitude_baro_ft, s.max_altitude_baro_ft,
               round(t.median_track_deg) as median_track_deg,
               case when t.last_alt - t.first_alt > 500 then 'climbing'
                    when t.first_alt - t.last_alt > 500 then 'descending' else 'level' end as vertical
        from s join {table("pub_aircraft")} a using (address_type, address)
        left join tracks t on t.address = s.address and t.sighting_number = s.sighting_number
        order by s.started_at""",
        start=start,
        end=end,
        prefix=prefix.strip().upper(),
    )
    return {
        "prefix": prefix.strip().upper(),
        "start_date": start,
        "end_date": (date.fromisoformat(end) - timedelta(days=1)).isoformat(),
        "sightings": len(rows),
        "aircraft": len({r["address"] for r in rows}),
        "note": "median_track_deg: 0 north, 90 east, 180 south, 270 west; null without positions.",
        "rows": rows,
    }


@mcp.tool(annotations=READ_ONLY)
def operator_fleets(
    start_date: str = "", end_date: str = "", min_aircraft: int = 2, limit: int = 30
) -> list[dict[str, Any]]:
    """Organizations (registered owner organization, else operator) with
    several aircraft heard in a period (Pacific dates, inclusive): how
    many aircraft, sightings and which types. Finds flight schools, charter
    and fractional fleets, airlines and government fleets. Private owners are
    never named, so their aircraft do not appear here."""
    start, end = date_range(start_date, end_date)
    return query(
        f"""
        with s as (
            select address_type, address, count(*) as sightings from {table("pub_sightings")}
            where is_confirmed and {IN_RANGE.format(col="started_at")}
            group by address_type, address)
        select coalesce(a.owner_name, a.operator) as organization,
               any_value(a.owner_category) as owner_category,
               count(*) as aircraft, sum(s.sightings) as sightings,
               array_agg(distinct a.type_name ignore nulls limit 6) as types,
               array_agg(coalesce(a.registration, a.address) order by s.sightings desc limit 8) as examples,
               countif(a.is_ladd) as ladd_listed
        from s join {table("pub_aircraft")} a using (address_type, address)
        where coalesce(a.owner_name, a.operator) is not null
        group by organization having count(*) >= @min
        order by sightings desc limit @limit""",
        start=start,
        end=end,
        min=clamp(min_aircraft, 1, 50),
        limit=clamp(limit, 1, 50),
    )


@mcp.tool(annotations=READ_ONLY)
def busiest_areas(
    start_date: str = "", end_date: str = "", max_altitude_ft: int = 8000, limit: int = 10
) -> list[dict[str, Any]]:
    """Where aircraft spend time at low altitude in a period (Pacific dates,
    inclusive): roughly 3 by 3 nm grid cells ranked by airborne snapshots
    below max_altitude_ft, with distinct aircraft, their circling and the
    organizations seen. Finds training areas, helicopter work and approaches."""
    start, end = date_range(start_date, end_date)
    return query(
        f"""
        with p as (
            select p.*, round(p.latitude / 0.05) * 0.05 as lat_cell,
                   round(p.longitude / 0.05) * 0.05 as lon_cell
            from {table("pub_snapshots")} p
            where {IN_RANGE.format(col="p.bucket_start")} and p.latitude is not null
              and p.altitude_baro_ft < @max_alt and not coalesce(p.on_ground, false))
        select round(p.lat_cell, 2) as latitude, round(p.lon_cell, 2) as longitude,
               count(*) as snapshots, count(distinct p.address) as aircraft,
               round(avg(p.altitude_baro_ft)) as avg_altitude_baro_ft,
               array_agg(distinct coalesce(a.owner_name, a.operator) ignore nulls limit 5) as organizations,
               array_agg(distinct a.type_name ignore nulls limit 5) as types
        from p left join {table("pub_aircraft")} a using (address_type, address)
        group by 1, 2 order by snapshots desc limit @limit""",
        start=start,
        end=end,
        max_alt=clamp(max_altitude_ft, 500, 20000),
        limit=clamp(limit, 1, 30),
    )


# ---------------------------------------------------------------- operations


@mcp.tool(annotations=READ_ONLY)
def receiver_status() -> dict[str, Any]:
    """Is the station working? Freshness of incoming data, listening time over
    the last 24 hours, and the latest scheduled data builds."""
    freshness = query(f"""
        select check_name, latest_status, latest_age_seconds, last_checked_at
        from {table("pub_data_health")} where check_type = 'source'""")
    uptime = query(f"""
        select round(avg(uptime_ratio), 3) as avg_uptime_ratio_24h,
               countif(uptime_ratio < 0.5) as hours_mostly_down_24h,
               round(sum(listening_seconds_1090) / 3600, 1) as hours_on_1090,
               round(sum(listening_seconds_978) / 3600, 1) as hours_on_978
        from {table("pub_receiver_uptime_hourly")}
        where hour_start >= timestamp_sub(current_timestamp(), interval 24 hour)""")
    runs = query(f"""
        select mode, started_at, passed, errors, warnings
        from {table("pub_dbt_runs")} order by started_at desc limit 5""")
    return {"freshness": freshness, "uptime": uptime[0] if uptime else None, "recent_builds": runs}


@mcp.tool(annotations=READ_ONLY)
def data_quality_status() -> dict[str, Any]:
    """Results of the data tests and freshness checks run on every build:
    counts by status and every check that is not passing."""
    checks = query(f"""
        select check_type, check_name, latest_status, latest_failures, last_checked_at,
               failed_runs_7d, warned_runs_7d
        from {table("pub_data_health")} order by latest_status, check_name""")
    summary: dict[str, int] = {}
    for c in checks:
        summary[c["latest_status"]] = summary.get(c["latest_status"], 0) + 1
    return {"summary": summary, "not_passing": [c for c in checks if c["latest_status"] != "pass"]}


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
)
def run_sql(sql: str, ctx: Context) -> dict[str, Any]:
    """Run a read-only BigQuery SELECT over the public views (requires the
    X-Sky-Scanner-Token header). Reference views as `public.<view>` or just
    `<view>`. The query is dry-run first: it must be a single SELECT within this
    project, scanning at most 1 GB. Returns at most 200 rows. Use
    describe_dataset for columns.

    The server's BigQuery account can read only the public views (BigQuery
    refuses anything else), so private tables stay private whatever the SQL."""
    supplied = (ctx.headers or {}).get("x-sky-scanner-token", "")
    if not SQL_TOKEN or not hmac.compare_digest(supplied, SQL_TOKEN):
        raise ToolError(
            "run_sql needs a valid X-Sky-Scanner-Token header. Use the other tools, which cover most questions."
        )
    rate_limit()
    try:
        dry = client().query(
            sql,
            job_config=bigquery.QueryJobConfig(
                dry_run=True, use_query_cache=False, default_dataset=f"{PROJECT}.{DATASET}"
            ),
        )
        check_dry_run(dry.statement_type, dry.referenced_tables, dry.total_bytes_processed)
        job = client().query(
            sql,
            job_config=bigquery.QueryJobConfig(
                maximum_bytes_billed=SQL_MAX_BYTES,
                default_dataset=f"{PROJECT}.{DATASET}",
                labels={"component": "mcp", "tool": "run_sql"},
            ),
        )
        result = job.result(max_results=MAX_ROWS)
    except GoogleAPICallError as exc:
        raise ToolError(f"BigQuery rejected the query: {exc.message}") from None
    rows = [jsonable(dict(r.items())) for r in result]
    return {
        "rows": rows,
        "row_count": len(rows),
        "bytes_billed": job.total_bytes_billed,
        "truncated": (result.total_rows or 0) > MAX_ROWS,
    }


def check_dry_run(statement_type: str, referenced_tables, bytes_processed: int | None) -> None:
    """Refuse non-SELECT statements, other projects and large scans.

    A dry run lists the tables underneath views, so it cannot tell a public
    view from a private table; IAM does that (see run_sql). It does catch
    other projects' public datasets, which would be billed to this one.
    """
    if statement_type != "SELECT":
        raise ToolError(f"Only SELECT statements are allowed, not {statement_type}.")
    outside = sorted({f"{t.project}.{t.dataset_id}" for t in referenced_tables if t.project != PROJECT})
    if outside:
        raise ToolError(f"Only this project's public views may be queried; not {', '.join(outside)}.")
    if (bytes_processed or 0) > SQL_MAX_BYTES:
        raise ToolError(
            f"Query would scan {bytes_processed:,} bytes; the limit is "
            f"{SQL_MAX_BYTES:,}. Filter on bucket_start or started_at."
        )


# ---------------------------------------------------------------- serving


# Cloud Run reserves /healthz for itself, so the probe uses /health.
@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    from starlette.responses import JSONResponse

    return JSONResponse({"status": "ok"})


def http_app():
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.responses import PlainTextResponse

    app = mcp.streamable_http_app(
        stateless_http=True,  # any Cloud Run instance can answer any request
        json_response=True,
        # DNS-rebinding protection guards servers on localhost; this one is
        # public by design and reachable under two hostnames.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host="0.0.0.0",
    )
    refuse_get = PlainTextResponse("Method Not Allowed", status_code=405, headers={"Allow": "POST"})

    async def no_get_stream(scope, receive, send):
        # The server never pushes messages, so refuse the optional GET stream
        # (the spec allows 405). Clients hold it open until Cloud Run's
        # request timeout and reconnect, which kept an instance billed all
        # day while any client was connected.
        if scope["type"] == "http" and scope["method"] == "GET" and scope["path"].rstrip("/") == "/mcp":
            await refuse_get(scope, receive, send)
            return
        await app(scope, receive, send)

    return no_get_stream


def main() -> None:
    if os.environ.get("MCP_TRANSPORT", "stdio") == "stdio":
        mcp.run("stdio")
        return
    import uvicorn

    uvicorn.run(
        http_app(),
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080)),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
