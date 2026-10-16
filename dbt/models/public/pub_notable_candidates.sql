-- Top notable candidates for the agent (via the MCP server) and the website.
select
    window_start, window_end,
    address_type, address, registration, designator, type_name, aircraft_class, aircraft_kind,
    operator, owner_name, country, year_manufactured, is_military, military_rules, is_ladd, is_pia,
    latest_callsign, first_seen_at, sightings_7d, days_seen_7d, messages_7d,
    max_circling_ratio_7d, low_away_snapshots_7d, emergency_code_7d, max_altitude_baro_ft_7d,
    min_altitude_baro_ft_7d, max_ground_speed_kt_7d, sightings_prior_28d, type_aircraft_heard,
    type_first_seen_this_week, notable_score, type_score, military_score, ownership_score,
    foreign_score, vintage_score, path_score, frequency_score, emergency_score, reasons,
    rank_within_type, candidates_of_type
from {{ ref('fct_notable_candidates') }}
