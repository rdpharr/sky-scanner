-- Why each aircraft counts as military. An aircraft is military when any
-- rule fires; every rule that fired is kept as evidence.
with identity as (
    select * from {{ ref('int_aircraft_identity') }}
),

operator_codes as (
    -- Callsigns that start with a military ICAO operator code, e.g. RCH123.
    select i.address_type, i.address,
           array_agg(distinct m.operator_name order by m.operator_name) as operator_names
    from identity as i, unnest(i.callsigns) as callsign
    inner join {{ ref('stg_military_operator_codes') }} as m
        on regexp_contains(callsign, r'^[A-Z]{3}[0-9]')
       and m.operator_code = substr(callsign, 1, 3)
    group by i.address_type, i.address
),

rules as (
    select
        i.address_type,
        i.address,
        i.plane_alert_operator,
        c.operator_names,
        regexp_contains(i.address, r'^A[EF]') as us_military_block,
        -- US Coast Guard aircraft fly as "C" plus the tail number.
        regexp_contains(i.address, r'^A[EF]')
            and exists(select 1 from unnest(i.callsigns) as cs where regexp_contains(cs, r'^C\d{4}$'))
            as coast_guard_callsign,
        array_concat(
            if(regexp_contains(i.address, r'^A[EF]'), ['us_military_address_block'], []),
            if(i.on_plane_alert, ['plane_alert_db'], []),
            if(i.tar1090_military, ['tar1090_military_flag'], []),
            if(c.operator_names is not null, ['military_operator_code'], [])
        ) as base_rules
    from identity as i
    left join operator_codes as c using (address_type, address)
)

select
    address_type,
    address,
    array_length(base_rules) > 0 as is_military,
    array_concat(base_rules,
                 if(coast_guard_callsign, ['us_coast_guard_callsign'], [])) as military_rules,
    if(array_length(base_rules) > 0,
       coalesce(plane_alert_operator, operator_names[safe_offset(0)],
                if(coast_guard_callsign, 'US Coast Guard', null)),
       null) as military_operator
from rules
