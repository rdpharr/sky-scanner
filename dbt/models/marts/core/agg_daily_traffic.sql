-- Daily traffic per band, Pacific time. Rates divide by time actually spent
-- listening on that band, so dwell changes and outages don't distort them.
with snapshots as (
    select
        date(s.bucket_start, 'America/Los_Angeles') as day,
        s.band_mhz,
        s.address_type,
        s.address,
        s.message_count
    from {{ ref('fct_snapshots') }} as s
),

aircraft as (
    select
        address_type,
        address,
        is_confirmed,
        is_military,
        identity_source,
        first_seen_at
    from {{ ref('dim_aircraft') }}
),

listening as (
    select
        date(started_at, 'America/Los_Angeles') as day,
        band_mhz,
        sum(listening_seconds) as listening_seconds
    from {{ ref('stg_capture_intervals') }}
    where ended_at is not null
    group by day, band_mhz
),

daily as (
    select
        s.day,
        s.band_mhz,
        count(distinct if(a.is_confirmed, s.address, null)) as confirmed_aircraft,
        count(distinct if(a.is_confirmed and a.is_military, s.address, null)) as military_aircraft,
        count(distinct if(a.is_confirmed and a.identity_source is not null, s.address, null))
            as identified_aircraft,
        count(distinct if(
            a.is_confirmed
            and date(a.first_seen_at, 'America/Los_Angeles') = s.day, s.address, null
        ))
            as first_seen_aircraft,
        count(*) as snapshots,
        sum(s.message_count) as messages
    from snapshots as s
    inner join aircraft as a using (address_type, address)
    group by s.day, s.band_mhz
)

select
    d.*,
    safe_divide(d.identified_aircraft, d.confirmed_aircraft) as identification_rate,
    l.listening_seconds,
    safe_divide(d.messages, l.listening_seconds / 3600) as messages_per_listening_hour,
    safe_divide(d.confirmed_aircraft, l.listening_seconds / 3600) as aircraft_per_listening_hour
from daily as d
left join listening as l using (day, band_mhz)
