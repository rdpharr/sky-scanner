-- Seconds spent listening on each band in each UTC hour, clipped to the hour.
with intervals as (
    select
        receiver_id,
        band_mhz,
        started_at,
        ended_at
    from {{ ref('stg_capture_intervals') }}
    where ended_at is not null
),

hours as (
    select hour_start
    from unnest(generate_timestamp_array(
        (select timestamp_trunc(min(started_at), hour) from intervals),
        (select timestamp_trunc(max(ended_at), hour) from intervals),
        interval 1 hour
    )) as hour_start
),

clipped as (
    select
        i.receiver_id,
        h.hour_start,
        i.band_mhz,
        timestamp_diff(
            least(i.ended_at, timestamp_add(h.hour_start, interval 1 hour)),
            greatest(i.started_at, h.hour_start), millisecond
        ) / 1000 as seconds
    from intervals as i
    inner join hours as h
        on
            i.started_at < timestamp_add(h.hour_start, interval 1 hour)
            and i.ended_at > h.hour_start
)

select
    receiver_id,
    hour_start,
    sum(if(band_mhz = 1090, seconds, 0)) as listening_seconds_1090,
    sum(if(band_mhz = 978, seconds, 0)) as listening_seconds_978,
    sum(seconds) as listening_seconds,
    -- Share of the hour spent listening on either band. Retuning gaps keep
    -- this below 100% even when the receiver runs all hour.
    sum(seconds) / 3600 as uptime_ratio
from clipped
group by receiver_id, hour_start
