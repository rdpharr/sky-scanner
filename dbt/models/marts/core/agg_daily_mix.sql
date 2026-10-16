-- Confirmed aircraft and sightings per Pacific day by class, kind and
-- identity source. Each aircraft has one class, kind and source, so summing
-- aircraft across any of them gives the day's distinct aircraft.
with heard as (
    select distinct
        date(bucket_start, 'America/Los_Angeles') as day,
        address_type,
        address
    from {{ ref('fct_snapshots') }}
),

sightings as (
    select
        date(started_at, 'America/Los_Angeles') as day,
        address_type,
        address,
        count(*) as sightings
    from {{ ref('fct_sightings') }}
    group by day, address_type, address
)

select
    h.day,
    a.aircraft_class,
    a.aircraft_kind,
    coalesce(a.identity_source, 'Unidentified') as identity_source,
    count(*) as confirmed_aircraft,
    coalesce(sum(s.sightings), 0) as sightings
from heard as h
inner join {{ ref('dim_aircraft') }} as a using (address_type, address)
left join sightings as s using (day, address_type, address)
where a.is_confirmed
group by h.day, a.aircraft_class, a.aircraft_kind, identity_source
