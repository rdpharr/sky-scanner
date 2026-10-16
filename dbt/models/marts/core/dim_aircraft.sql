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

type_rows as (
    -- The Doc 8643 row whose manufacturer appears in the aircraft's own
    -- description (FAA, tar1090-db or OpenSky), else the first row. Rows are
    -- alphabetical by manufacturer, so "first" alone often picks a licence
    -- builder (e.g. "Aero Eli Yo-Yo" for a Robinson R22).
    select
        i.address_type,
        i.address,
        r.manufacturer_code,
        r.model_full_name
    from {{ ref('int_aircraft_identity') }} as i
    inner join {{ ref('stg_doc8643_type_rows') }} as r on r.designator = i.designator
    qualify row_number() over (
        partition by i.address_type, i.address
        order by strpos(
            upper(coalesce(i.aircraft_description, '')),
            split(upper(r.manufacturer_code), ' ')[safe_offset(0)]
        ) > 0 desc,
        r._row_number
    ) = 1
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
        tr.manufacturer_code,
        tr.model_full_name,
        exists(
            select 1 from unnest(ap.prefixes) as prefix
            where starts_with(i.designator, prefix)
        ) as is_airliner_designator,
        ac.pattern as airline_callsign_pattern
    from {{ ref('int_aircraft_identity') }} as i
    inner join {{ ref('int_military_classification') }} as m using (address_type, address)
    left join {{ ref('stg_doc8643_types') }} as t on t.designator = i.designator
    left join {{ ref('icao_type_names') }} as n on n.designator = i.designator
    left join type_rows as tr on tr.address_type = i.address_type and tr.address = i.address
    cross join airliner_prefixes as ap
    cross join airline_callsigns as ac
),

with_class as (
    select
        *,
        case
            when designator is null then 'Unidentified'
            when doc8643_description is null
                then
                    if(is_airliner_designator, 'Narrow-body / regional jet', 'Other')
            when regexp_contains(doc8643_description, r'^[HG]') then 'Helicopter'
            when ends_with(doc8643_description, 'P') then 'Piston'
            when ends_with(doc8643_description, 'T') then 'Turboprop'
            when ends_with(doc8643_description, 'J')
                then
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
        -- For military airframes the lists' own names are more specific
        -- (e.g. "Boeing VC-25A" rather than Doc 8643's "Boeing 747-200").
        when c.is_military and c.plane_alert_model is not null then {{ tidy_model_name('c.plane_alert_model') }}
        when c.is_military and c.tar1090_model is not null then {{ tidy_model_name('c.tar1090_model') }}
        else coalesce(
            c.curated_type_name,
            nullif(trim(concat(
                initcap(coalesce(c.manufacturer_code, '')), ' ',
                coalesce(c.model_full_name, '')
            )), ''),
            c.aircraft_description, 'Unidentified'
        )
    end as type_name,
    c.aircraft_description,
    c.aircraft_class,
    case
        when c.is_military then 'Military'
        when
            regexp_contains(coalesce(c.latest_callsign, ''), c.airline_callsign_pattern)
            or regexp_contains(
                concat(coalesce(c.owner, ''), ' ', coalesce(c.civil_operator, '')),
                r'(?i)AIR ?LINES|AIRWAYS|AIRLINE|FEDERAL EXPRESS|UNITED PARCEL|KOREAN AIR|AIR FRANCE|AIR CANADA|PORTER|SWOOP|TAP PORTUGAL'  -- noqa: LT05
            )
            or (
                c.aircraft_class in ('Narrow-body / regional jet', 'Wide-body jet')
                and (c.owner is null or regexp_contains(c.owner, r'(?i)BANK|TRUST|ASSET MANAGEMENT|LEASING'))
            )
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
