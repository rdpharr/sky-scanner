-- Aircraft for the website, MCP server and agent. Aircraft on the FAA LADD
-- list and Privacy ICAO Addresses (PIA) are included and flagged (decided
-- 2026-10-16, as public ADS-B sites do); private owners' names are never shown.
select
    address_type,
    address,
    registration,
    designator,
    type_name,
    aircraft_description,
    aircraft_class,
    aircraft_kind,
    identity_source,
    {{ public_owner('owner', 'owner_source', 'faa_registrant_is_organization', 'faa_registrant_type') }} as owner_name,
    case
        when owner is null then 'Unknown'
        when {{ public_owner('owner', 'owner_source', 'faa_registrant_is_organization', 'faa_registrant_type') }} is not null
            then coalesce(registrant_category, 'Organization')
        else 'Private or unverified'
    end as owner_category,
    operator,
    country,
    year_manufactured,
    is_military,
    military_rules,
    is_ladd,
    is_pia,
    is_miscode,
    latest_callsign,
    callsigns,
    first_seen_at,
    last_seen_at,
    snapshots,
    messages,
    is_confirmed,
    bands_mhz,
    max_altitude_baro_ft,
    positioned_snapshots
from {{ ref('dim_aircraft') }}
