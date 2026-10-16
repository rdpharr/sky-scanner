{{ config(severity='warn') }}
-- Every snapshot in the last two days was received while the radio was
-- listening on its band (5 s tolerance for clock and flush timing).
with recent as (
    select snapshot_id, receiver_id, band_mhz, first_received_at
    from {{ ref('fct_snapshots') }}
    where bucket_start >= timestamp_sub(current_timestamp(), interval 2 day)
      and bucket_start < timestamp_sub(current_timestamp(), interval 10 minute)
),
turns as (
    select receiver_id, band_mhz, started_at, coalesce(ended_at, current_timestamp()) as ended_at
    from {{ ref('fct_capture_intervals') }}
    where started_at >= timestamp_sub(current_timestamp(), interval 3 day)
)
select r.snapshot_id
from recent as r
left join turns as t
    on t.receiver_id = r.receiver_id and t.band_mhz = r.band_mhz
   and r.first_received_at between timestamp_sub(t.started_at, interval 5 second)
                               and timestamp_add(t.ended_at, interval 5 second)
where t.receiver_id is null
