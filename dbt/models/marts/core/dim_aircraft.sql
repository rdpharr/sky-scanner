-- One row per aircraft address ever heard: identity, classification and activity.
with activity as (
    select
        address_type,
        address,
        min(first_received_at) as first_seen_at,
        max(last_received_at) as last_seen_at,
        count(*) as snapshots,
        sum(message_count) as messages,
        array_agg(distinct band_mhz order by band_mhz) as bands_mhz,
        max(altitude_baro_ft) as max_altitude_baro_ft,
        countif(position is not null) as positioned_snapshots
    from {{ ref('fct_snapshots') }}
    group by address_type, address
),

airliner_prefixes as (
    select array_agg(type_prefix) as prefixes from {{ ref('airliner_type_prefixes') }}
),

airline_callsigns as (
    select concat(r'^(', string_agg(prefix, '|'), r')\d') as pattern
    from {{ ref('airline_callsign_prefixes') }}
),

classified as (
    select
        i.*,
        m.is_military,
        m.military_rules,
        m.military_operator,
        t.description as doc8643_description,
        t.wake_category,
        n.type_name as curated_type_name,
        t.manufacturer_code,
        t.model_full_name,
        exists(select 1 from unnest(ap.prefixes) as prefix
               where starts_with(i.designator, prefix)) as is_airliner_designator,
        ac.pattern as airline_callsign_pattern
    from {{ ref('int_aircraft_identity') }} as i
    inner join {{ ref('int_military_classification') }} as m using (address_type, address)
    left join {{ ref('stg_doc8643_types') }} as t on t.designator = i.designator
    left join {{ ref('icao_type_names') }} as n on n.designator = i.designator
    cross join airliner_prefixes as ap
    cross join airline_callsigns as ac
),

with_class as (
    select
        *,
        case
            when designator is null then 'Unidentified'
            when doc8643_description is null then
                if(is_airliner_designator, 'Narrow-body / regional jet', 'Other')
            when regexp_contains(doc8643_description, r'^[HG]') then 'Helicopter'
            when ends_with(doc8643_description, 'P') then 'Piston'
            when ends_with(doc8643_description, 'T') then 'Turboprop'
            when ends_with(doc8643_description, 'J') then
                case
                    when wake_category in ('H', 'J') then 'Wide-body jet'
                    when is_airliner_designator then 'Narrow-body / regional jet'
                    else 'Business jet'
                end
            else 'Other'
        end as aircraft_class
    from classified
)

select
    c.address_type,
    c.address,
    c.registration,
    c.designator,
    case
        when c.is_miscode then 'Invalid transponder address (miscode)'
        -- For military airframes plane-alert-db's type name is more specific.
        when c.is_military and c.plane_alert_model is not null then c.plane_alert_model
        else coalesce(c.curated_type_name,
                      nullif(trim(concat(initcap(coalesce(c.manufacturer_code, '')), ' ',
                                         coalesce(c.model_full_name, ''))), ''),
                      c.aircraft_description, 'Unidentified')
    end as type_name,
    c.aircraft_description,
    c.aircraft_class,
    case
        when c.is_military then 'Military'
        when regexp_contains(coalesce(c.latest_callsign, ''), c.airline_callsign_pattern)
          or regexp_contains(concat(coalesce(c.owner, ''), ' ', coalesce(c.civil_operator, '')),
                r'(?i)AIR ?LINES|AIRWAYS|AIRLINE|FEDERAL EXPRESS|UNITED PARCEL|KOREAN AIR|AIR FRANCE|AIR CANADA|PORTER|SWOOP|TAP PORTUGAL')
          or (c.aircraft_class in ('Narrow-body / regional jet', 'Wide-body jet')
              and (c.owner is null or regexp_contains(c.owner, r'(?i)BANK|TRUST|ASSET MANAGEMENT|LEASING')))
            then 'Airline'
        else 'Other'
    end as aircraft_kind,
    c.identity_source,
    c.owner,
    c.owner_source,
    c.faa_registrant_type,
    r.registrant_category,
    r.is_organization as faa_registrant_is_organization,
    if(c.is_military, coalesce(c.military_operator, c.civil_operator), c.civil_operator) as operator,
    c.opensky_country as country,
    c.faa_year_mfr as year_manufactured,
    c.is_military,
    c.military_rules,
    c.is_ladd,
    c.is_pia,
    c.is_miscode,
    c.latest_callsign,
    c.callsigns,
    a.first_seen_at,
    a.last_seen_at,
    a.snapshots,
    a.messages,
    a.messages >= {{ var('confirmed_min_messages') }} as is_confirmed,
    a.bands_mhz,
    a.max_altitude_baro_ft,
    a.positioned_snapshots
from with_class as c
inner join activity as a using (address_type, address)
left join {{ ref('faa_registrant_types') }} as r on r.registrant_type = c.faa_registrant_type
