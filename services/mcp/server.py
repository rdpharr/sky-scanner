"""Sky Scanner MCP server: read-only tools over the BigQuery `public` dataset.

Runs over Streamable HTTP on Cloud Run (MCP_TRANSPORT=http) or stdio for a
local client (the default). It authenticates to BigQuery as sky-mcp, which can
read only the public views: no tool can reach owner names, LADD/PIA aircraft
or raw tables, whatever it is asked. Every query is parameterized and capped
in bytes billed.
"""

from __future__ import annotations

import hmac
import os
import threading
import time
from collections import deque
from datetime import date, datetime
from decimal import Decimal
from typing import Any

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
- Aircraft whose owners opted out of display (FAA LADD) are excluded; their
  absence is not evidence they were not there.
- Coverage is partial: many aircraft report no position, the radio splits time
  between two bands, and outages happen (see receiver_status).
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                            idempotent_hint=True, open_world_hint=False)

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


def jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, list):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
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


def resolve(identifier: str) -> dict[str, Any]:
    """Find one public aircraft by address, registration or callsign."""
    rows = query(f"""
        select address_type, address
        from {table('pub_aircraft')}
        where address = @id or upper(registration) = @id or latest_callsign = @id
           or @id in unnest(callsigns)
        order by (address = @id) desc, (upper(registration) = @id) desc, last_seen_at desc
        limit 1""", id=identifier.strip().upper())
    if not rows:
        raise ToolError(f"No public aircraft matches {identifier!r}. Try search_aircraft. "
                         "(Aircraft on the FAA LADD list are never available.)")
    return rows[0]


# ---------------------------------------------------------------- context

@mcp.tool(annotations=READ_ONLY)
def get_definitions(term: str = "") -> dict[str, Any]:
    """Canonical definitions of metrics and public columns, exported from dbt.

    With no term: every metric plus a one-line description of each public view.
    With a term (e.g. "sighting", "confirmed", "circling"): metrics and columns
    whose name or definition mentions it. Prefer these definitions to your own.
    """
    rows = query(f"""
        select kind, name, model, label, description, metric_type, formula, filter
        from {table('pub_definitions')}
        where kind = 'metric'
           or (@term = '' and kind = 'model')
           or (@term != '' and (lower(name) like concat('%', @term, '%')
                                or lower(description) like concat('%', @term, '%')))
        order by kind, model, name""", term=term.strip().lower())
    metrics = [r for r in rows if r["kind"] == "metric"
               and (not term or term.lower() in (r["name"] + (r["description"] or "")).lower())]
    if not term:
        views = [{"view": r["name"], "description": r["description"]} for r in rows if r["kind"] == "model"]
        return {"metrics": metrics, "views": views}
    columns = [{"view": r["model"], "column": r["name"], "description": r["description"]}
               for r in rows if r["kind"] == "column"]
    return {"metrics": metrics, "columns": columns}


@mcp.tool(annotations=READ_ONLY)
def describe_dataset(view: str = "") -> dict[str, Any]:
    """The public BigQuery views this server reads, with every column's meaning.

    Give a view name (e.g. "pub_sightings") for its columns, or nothing for
    the list of views. Useful before run_sql.
    """
    rows = query(f"""
        select kind, name, model, description from {table('pub_definitions')}
        where (@view = '' and kind = 'model') or (@view != '' and model = @view)
        order by model, kind desc""", view=view.strip())
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
def search_aircraft(text: str = "", kind: str = "", military_only: bool = False,
                    heard_within_days: int = 30, limit: int = 20) -> list[dict[str, Any]]:
    """Find confirmed aircraft by registration, callsign, type, operator or address.

    kind: "Airline", "Military" or "Other" (empty for all). heard_within_days
    filters on last_seen_at (0 for all time). Newest first, at most 50.
    """
    return query(f"""
        select {AIRCRAFT_COLUMNS} from {table('pub_aircraft')}
        where is_confirmed
          and (@text = '' or upper(concat(ifnull(registration,''), ' ', ifnull(latest_callsign,''), ' ',
               ifnull(type_name,''), ' ', ifnull(operator,''), ' ', address, ' ', ifnull(designator,'')))
               like concat('%', @text, '%'))
          and (@kind = '' or aircraft_kind = @kind)
          and (not @military_only or is_military)
          and (@days = 0 or last_seen_at >= timestamp_sub(current_timestamp(), interval @days day))
        order by last_seen_at desc limit @limit""",
        text=text.strip().upper(), kind=kind.strip().capitalize(), military_only=military_only,
        days=clamp(heard_within_days, 0, 3650), limit=clamp(limit, 1, 50))


@mcp.tool(annotations=READ_ONLY)
def get_aircraft(identifier: str) -> dict[str, Any]:
    """Everything known about one aircraft: identity (with its source),
    classification, military evidence, lifetime activity and recent sightings.

    identifier: ICAO address (ADFDF9), registration (N969NN) or callsign (SWA2813).
    """
    key = resolve(identifier)
    aircraft = query(f"""select {AIRCRAFT_COLUMNS}, is_confirmed from {table('pub_aircraft')}
                         where address_type = @t and address = @a""",
                     t=key["address_type"], a=key["address"])[0]
    aircraft["recent_sightings"] = query(f"""
        select started_at, ended_at, duration_minutes, messages, callsigns, min_altitude_baro_ft,
               max_altitude_baro_ft, max_ground_speed_kt, positioned_snapshots, circling_ratio,
               emergency_squawk
        from {table('pub_sightings')} where address_type = @t and address = @a
        order by started_at desc limit 5""", t=key["address_type"], a=key["address"])
    if aircraft["is_military"] and aircraft["callsigns"]:
        # War Watch Intel callsign families (e.g. TALON for TALON95): supporting
        # evidence only, since callwords are shared across units.
        lookups = query(f"""select callword, lookup_status, source_url, lookup_data
                            from {table('pub_callword_lookups')}""")
        aircraft["callword_lookups"] = [
            r for r in lookups if any(cs.startswith(r["callword"]) for cs in aircraft["callsigns"])]
    return aircraft


@mcp.tool(annotations=READ_ONLY)
def get_sightings(identifier: str, days: int = 30, limit: int = 50) -> list[dict[str, Any]]:
    """An aircraft's sightings (runs of reports with no gap over 10 minutes),
    newest first, with altitude, speed and flight-path features."""
    key = resolve(identifier)
    return query(f"""
        select sighting_number, started_at, ended_at, duration_minutes, snapshots, messages,
               bands_mhz, callsigns, squawks, min_altitude_baro_ft, max_altitude_baro_ft,
               max_ground_speed_kt, positioned_snapshots, path_nm, displacement_nm, circling_ratio,
               total_turn_deg, low_away_from_airport_snapshots, closest_airport_nm, emergency_squawk
        from {table('pub_sightings')}
        where address_type = @t and address = @a
          and started_at >= timestamp_sub(current_timestamp(), interval @days day)
        order by started_at desc limit @limit""",
        t=key["address_type"], a=key["address"], days=clamp(days, 1, 3650), limit=clamp(limit, 1, 200))


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
    rows = query(f"""
        select * except (address_type, address, sighting_id)
        from {table('pub_sightings')}
        where address_type = @t and address = @a
          and (@start = '' or started_at = timestamp(@start))
        order by started_at desc limit 1""",
        t=key["address_type"], a=key["address"], start=sighting_started_at.strip())
    if not rows:
        raise ToolError("No such sighting; list them with get_sightings.")
    sighting = rows[0]
    stats = query(f"""
        select count(*) as snapshots,
               round(avg(altitude_baro_ft)) as avg_altitude_baro_ft,
               round(avg(ground_speed_kt)) as avg_ground_speed_kt,
               round(min(latitude), 2) as south, round(max(latitude), 2) as north,
               round(min(longitude), 2) as west, round(max(longitude), 2) as east
        from {table('pub_snapshots')}
        where address_type = @t and address = @a
          and bucket_start between timestamp_sub(timestamp(@start), interval 15 second)
                               and timestamp(@end)""",
        t=key["address_type"], a=key["address"], start=sighting["started_at"], end=sighting["ended_at"])
    return {"address": key["address"], "sighting": sighting, "track": stats[0] if stats else None}


@mcp.tool(annotations=READ_ONLY)
def get_type_info(designator: str) -> dict[str, Any]:
    """An aircraft type (ICAO designator, e.g. B738, H60, VC25): class, engines,
    how many public aircraft of it were heard and how rare it is here, with
    example aircraft and a Wikipedia article."""
    rows = query(f"""
        with ranked as (
            select *, rank() over (order by aircraft_heard desc) as popularity_rank,
                   count(*) over () as types_heard
            from {table('pub_aircraft_types')})
        select * from ranked where designator = @d""", d=designator.strip().upper())
    if not rows:
        raise ToolError(f"No public aircraft of type {designator!r} has been heard.")
    info = rows[0]
    info["examples"] = query(f"""
        select registration, latest_callsign, operator, aircraft_kind, last_seen_at
        from {table('pub_aircraft')} where designator = @d and is_confirmed
        order by last_seen_at desc limit 5""", d=info["designator"])
    return info


@mcp.tool(annotations=READ_ONLY)
def get_history(identifier: str) -> dict[str, Any]:
    """How often an aircraft is heard: sightings per week over all time, days
    seen, and the hours of day (Pacific) it is usually heard. Use it to tell a
    regular from a first-time or unusual visitor."""
    key = resolve(identifier)
    weekly = query(f"""
        select date_trunc(date(started_at, 'America/Los_Angeles'), week(monday)) as week_starting,
               count(*) as sightings, sum(messages) as messages
        from {table('pub_sightings')} where address_type = @t and address = @a
        group by week_starting order by week_starting""", t=key["address_type"], a=key["address"])
    hours = query(f"""
        select extract(hour from started_at at time zone 'America/Los_Angeles') as hour_pt,
               count(*) as sightings
        from {table('pub_sightings')} where address_type = @t and address = @a
        group by hour_pt order by hour_pt""", t=key["address_type"], a=key["address"])
    days = query(f"""
        select count(distinct date(started_at, 'America/Los_Angeles')) as days_seen,
               min(started_at) as first_sighting, max(started_at) as latest_sighting
        from {table('pub_sightings')} where address_type = @t and address = @a""",
        t=key["address_type"], a=key["address"])[0]
    return {"address": key["address"], **days, "weekly": weekly, "hours_pt": hours,
            "note": "Collection began 2026-09-26, so 'first seen' before late November "
                    "2026 does not mean the aircraft is new to the area."}


# ---------------------------------------------------------------- activity

@mcp.tool(annotations=READ_ONLY)
def recent_activity(minutes: int = 10) -> dict[str, Any]:
    """Aircraft heard in the last N minutes (1-30), seconds-fresh: latest
    position, altitude and speed per aircraft. Identity fields are null for
    aircraft first heard since the last hourly build."""
    minutes = clamp(minutes, 1, 30)
    aircraft = query(f"""
        select address, any_value(callsign having max bucket_start) as callsign,
               any_value(registration) as registration, any_value(type_name) as type_name,
               any_value(aircraft_kind) as aircraft_kind, any_value(operator) as operator,
               max(last_received_at) as last_heard,
               any_value(altitude_baro_ft having max bucket_start) as altitude_baro_ft,
               any_value(ground_speed_kt having max bucket_start) as ground_speed_kt,
               any_value(latitude having max bucket_start) as latitude,
               any_value(longitude having max bucket_start) as longitude,
               sum(message_count) as messages
        from {table('pub_live_snapshots')}
        where bucket_start >= timestamp_sub(current_timestamp(), interval @m minute)
        group by address order by last_heard desc""", m=minutes)
    return {"minutes": minutes, "aircraft_count": len(aircraft), "aircraft": aircraft}


@mcp.tool(annotations=READ_ONLY)
def traffic_stats(days: int = 7) -> list[dict[str, Any]]:
    """Daily traffic per band (Pacific days): confirmed, military and identified
    aircraft, messages, and rates per listening hour. Compare days with the
    per-listening-hour rates; raw counts depend on time spent on each band."""
    return query(f"""
        select * from {table('pub_daily_traffic')}
        where day >= date_sub(current_date('America/Los_Angeles'), interval @d day)
        order by day desc, band_mhz""", d=clamp(days, 1, 365))


@mcp.tool(annotations=READ_ONLY)
def military_sightings(days: int = 7) -> list[dict[str, Any]]:
    """Military aircraft heard in the last N days, with type, operator, the
    rules that classified them as military and their sightings."""
    return query(f"""
        with recent as (
            select address_type, address, count(*) as sightings, min(started_at) as first_sighting,
                   max(ended_at) as latest_sighting, max(max_altitude_baro_ft) as max_altitude_baro_ft
            from {table('pub_sightings')}
            where is_military and started_at >= timestamp_sub(current_timestamp(), interval @d day)
            group by address_type, address)
        select a.address, a.registration, a.type_name, a.designator, a.operator, a.military_rules,
               a.callsigns, r.sightings, r.first_sighting, r.latest_sighting, r.max_altitude_baro_ft
        from recent r join {table('pub_aircraft')} a using (address_type, address)
        where a.is_confirmed
        order by r.latest_sighting desc""", d=clamp(days, 1, 365))


# ---------------------------------------------------------------- operations

@mcp.tool(annotations=READ_ONLY)
def receiver_status() -> dict[str, Any]:
    """Is the station working? Freshness of incoming data, listening time over
    the last 24 hours, and the latest scheduled data builds."""
    freshness = query(f"""
        select check_name, latest_status, latest_age_seconds, last_checked_at
        from {table('pub_data_health')} where check_type = 'source'""")
    uptime = query(f"""
        select round(avg(uptime_ratio), 3) as avg_uptime_ratio_24h,
               countif(uptime_ratio < 0.5) as hours_mostly_down_24h,
               round(sum(listening_seconds_1090) / 3600, 1) as hours_on_1090,
               round(sum(listening_seconds_978) / 3600, 1) as hours_on_978
        from {table('pub_receiver_uptime_hourly')}
        where hour_start >= timestamp_sub(current_timestamp(), interval 24 hour)""")
    runs = query(f"""
        select mode, started_at, passed, errors, warnings
        from {table('pub_dbt_runs')} order by started_at desc limit 5""")
    return {"freshness": freshness, "uptime": uptime[0] if uptime else None, "recent_builds": runs}


@mcp.tool(annotations=READ_ONLY)
def data_quality_status() -> dict[str, Any]:
    """Results of the data tests and freshness checks run on every build:
    counts by status and every check that is not passing."""
    checks = query(f"""
        select check_type, check_name, latest_status, latest_failures, last_checked_at,
               failed_runs_7d, warned_runs_7d
        from {table('pub_data_health')} order by latest_status, check_name""")
    summary: dict[str, int] = {}
    for c in checks:
        summary[c["latest_status"]] = summary.get(c["latest_status"], 0) + 1
    return {"summary": summary, "not_passing": [c for c in checks if c["latest_status"] != "pass"]}


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                      idempotent_hint=True, open_world_hint=False))
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
        raise ToolError("run_sql needs a valid X-Sky-Scanner-Token header. Use the other tools, "
                         "which cover most questions.")
    rate_limit()
    try:
        dry = client().query(sql, job_config=bigquery.QueryJobConfig(
            dry_run=True, use_query_cache=False, default_dataset=f"{PROJECT}.{DATASET}"))
        check_dry_run(dry.statement_type, dry.referenced_tables, dry.total_bytes_processed)
        job = client().query(sql, job_config=bigquery.QueryJobConfig(
            maximum_bytes_billed=SQL_MAX_BYTES, default_dataset=f"{PROJECT}.{DATASET}",
            labels={"component": "mcp", "tool": "run_sql"}))
        result = job.result(max_results=MAX_ROWS)
    except GoogleAPICallError as exc:
        raise ToolError(f"BigQuery rejected the query: {exc.message}") from None
    rows = [jsonable(dict(r.items())) for r in result]
    return {"rows": rows, "row_count": len(rows), "bytes_billed": job.total_bytes_billed,
            "truncated": (result.total_rows or 0) > MAX_ROWS}


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
        raise ToolError(f"Query would scan {bytes_processed:,} bytes; the limit is "
                         f"{SQL_MAX_BYTES:,}. Filter on bucket_start or started_at.")


# ---------------------------------------------------------------- serving

# Cloud Run reserves /healthz for itself, so the probe uses /health.
@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ok"})


def main() -> None:
    if os.environ.get("MCP_TRANSPORT", "stdio") == "stdio":
        mcp.run("stdio")
        return
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    app = mcp.streamable_http_app(
        stateless_http=True,  # any Cloud Run instance can answer any request
        json_response=True,
        # DNS-rebinding protection guards servers on localhost; this one is
        # public by design and reachable under two hostnames.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host="0.0.0.0",
    )
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)),
                proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
