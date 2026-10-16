-- Current identity of every aircraft ever heard, resolved across sources.
-- Precedence follows the aircraft log: FAA (official) > tar1090-db (current,
-- community) > OpenSky (older crowdsourced snapshot) > plane-alert-db.
with observed as (
    select
        address_type,
        address,
        array_agg(callsign ignore nulls order by bucket_start desc limit 1)[safe_offset(0)]
            as latest_callsign,
        array_agg(distinct callsign ignore nulls) as callsigns
    from {{ ref('stg_aircraft_snapshots') }}
    group by address_type, address
),

latest as (
    select *
    from {{ ref('int_metadata_versions') }}
    where is_latest and registry_status = 'present'
),

faa as (
    select * from latest
    where source_name = 'faa_registry'
),

opensky as (
    select * from latest
    where source_name = 'opensky'
),

plane_alert as (
    select * from latest
    where source_name = 'plane_alert_mil'
),

tar1090 as (
    select
        *,
        -- "miscode" rows identify transponders broadcasting an invalid address;
        -- nothing else in them describes a real aircraft.
        'miscode' in unnest(split(coalesce(tags, ''), ', '))
        or starts_with(lower(coalesce(registration, '')), 'miscode')
        or starts_with(lower(coalesce(model, '')), 'miscode')
        or starts_with(lower(coalesce(icao_type_code, '')), 'miscode')
        or starts_with(lower(coalesce(registered_owner, '')), 'miscode') as is_miscode,
        split(coalesce(tags, ''), ', ') as tag_list
    from latest
    where source_name = 'tar1090_db'
),

joined as (
    select
        o.address_type,
        o.address,
        o.latest_callsign,
        o.callsigns,
        faa.registration as faa_registration,
        faa.manufacturer as faa_manufacturer,
        faa.model as faa_model,
        faa.registered_owner as faa_owner,
        faa.registrant_type as faa_registrant_type,
        faa.year_mfr as faa_year_mfr,
        coalesce(t.is_miscode, false) as is_miscode,
        if(t.is_miscode, null, t.registration) as tar1090_registration,
        if(t.is_miscode, null, t.icao_type_code) as tar1090_type_code,
        if(t.is_miscode, null, t.model) as tar1090_model,
        if(t.is_miscode, null, t.registered_owner) as tar1090_owner,
        coalesce(not t.is_miscode and 'LADD' in unnest(t.tag_list), false) as is_ladd,
        coalesce(not t.is_miscode and 'privacy ICAO address (PIA)' in unnest(t.tag_list), false)
            as is_pia,
        coalesce(not t.is_miscode and 'military' in unnest(t.tag_list), false)
            as tar1090_military,
        os.registration as opensky_registration,
        os.model as opensky_model,
        os.icao_type_code as opensky_type_code,
        os.operator as opensky_operator,
        os.operator_icao as opensky_operator_icao,
        os.registered_owner as opensky_owner,
        os.country as opensky_country,
        pa.registration as plane_alert_registration,
        pa.model as plane_alert_model,
        pa.icao_type_code as plane_alert_type_code,
        pa.operator as plane_alert_operator,
        pa.category as plane_alert_category,
        pa.tags as plane_alert_tags,
        coalesce(pa.operator, pa.model, pa.icao_type_code, pa.registration) is not null
            as on_plane_alert
    from observed as o
    left join faa using (address_type, address)
    left join tar1090 as t using (address_type, address)
    left join opensky as os using (address_type, address)
    left join plane_alert as pa using (address_type, address)
),

faa_designator as (
    -- FAA model text mapped to an ICAO designator by the first matching pattern.
    select
        j.address_type,
        j.address,
        p.designator
    from joined as j
    inner join {{ ref('faa_model_type_patterns') }} as p
        on regexp_contains(upper(j.faa_model), p.model_pattern)
    where j.faa_registration is not null
    qualify row_number() over (partition by j.address_type, j.address order by p.priority) = 1
)

select
    j.*,
    coalesce(f.designator, j.tar1090_type_code, j.opensky_type_code, j.plane_alert_type_code)
        as designator,
    coalesce(
        j.faa_registration, j.tar1090_registration, j.opensky_registration,
        j.plane_alert_registration
    ) as registration,
    case
        when j.faa_registration is not null then 'faa_registry'
        when coalesce(j.tar1090_registration, j.tar1090_type_code) is not null then 'tar1090_db'
        when coalesce(j.opensky_registration, j.opensky_model, j.opensky_type_code) is not null
            then 'opensky'
        when coalesce(j.plane_alert_registration, j.plane_alert_model) is not null
            then 'plane_alert_mil'
    end as identity_source,
    coalesce(j.faa_owner, j.tar1090_owner, j.opensky_owner) as owner,
    case
        when j.faa_owner is not null then 'faa_registry'
        when j.tar1090_owner is not null then 'tar1090_db'
        when j.opensky_owner is not null then 'opensky'
    end as owner_source,
    coalesce(j.opensky_operator, j.opensky_operator_icao) as civil_operator,
    coalesce(
        nullif(array_to_string([j.faa_manufacturer, j.faa_model], ' '), ''),
        j.tar1090_model, j.opensky_model, j.plane_alert_model
    ) as aircraft_description
from joined as j
left join faa_designator as f using (address_type, address)
