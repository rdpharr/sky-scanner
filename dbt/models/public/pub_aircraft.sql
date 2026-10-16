-- Aircraft for the website, MCP server and agent. Aircraft whose owners asked
-- not to be displayed (FAA LADD) or that use privacy ICAO addresses (PIA)
-- are excluded entirely; private owners' names are never shown.
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
    {{ public_owner('owner', 'owner_source', 'faa_registrant_is_organization') }} as owner_name,
    case
        when owner is null then 'Unknown'
        when {{ public_owner('owner', 'owner_source', 'faa_registrant_is_organization') }} is not null
            then coalesce(registrant_category, 'Organization')
        else 'Private or unverified'
    end as owner_category,
    operator,
    country,
    year_manufactured,
    is_military,
    military_rules,
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
where not is_ladd and not is_pia
