-- Traffic per UTC hour, with the hour's Pacific day, hour and weekday and the
-- receiver's listening time, so charts can normalize by uptime and shade
-- hours the receiver was mostly off. Hours with no listening are included.
with hours as (
    select hour_start, listening_seconds, uptime_ratio
    from {{ ref('fct_receiver_uptime_hourly') }}
),

aircraft as (
    select address_type, address, aircraft_kind
    from {{ ref('dim_aircraft') }}
    where is_confirmed
),

heard as (
    select
        timestamp_trunc(s.bucket_start, hour) as hour_start,
        count(distinct s.address) as confirmed_aircraft,
        count(distinct if(a.aircraft_kind = 'Airline', s.address, null)) as airline_aircraft,
        count(distinct if(a.aircraft_kind = 'Military', s.address, null)) as military_aircraft,
        count(distinct if(a.aircraft_kind = 'Other', s.address, null)) as other_aircraft,
        sum(s.message_count) as messages
    from {{ ref('fct_snapshots') }} as s
    inner join aircraft as a using (address_type, address)
    group by hour_start
),

sightings as (
    select timestamp_trunc(started_at, hour) as hour_start, count(*) as sightings_started
    from {{ ref('fct_sightings') }}
    where is_confirmed
    group by hour_start
)

select
    h.hour_start,
    date(h.hour_start, 'America/Los_Angeles') as day_pt,
    extract(hour from datetime(h.hour_start, 'America/Los_Angeles')) as hour_pt,
    format_datetime('%A', datetime(h.hour_start, 'America/Los_Angeles')) as weekday_pt,
    h.listening_seconds,
    h.uptime_ratio,
    coalesce(t.confirmed_aircraft, 0) as confirmed_aircraft,
    coalesce(t.airline_aircraft, 0) as airline_aircraft,
    coalesce(t.military_aircraft, 0) as military_aircraft,
    coalesce(t.other_aircraft, 0) as other_aircraft,
    coalesce(g.sightings_started, 0) as sightings_started,
    coalesce(t.messages, 0) as messages,
    safe_divide(t.confirmed_aircraft, h.listening_seconds / 3600) as aircraft_per_listening_hour
from hours as h
left join heard as t using (hour_start)
left join sightings as g using (hour_start)
