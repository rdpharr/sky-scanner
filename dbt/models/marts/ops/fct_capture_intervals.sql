-- Listening turns with the silent gap before each (retuning, decoder start-up, outages).
select
    *,
    timestamp_diff(
        started_at,
        lag(ended_at) over (partition by receiver_id order by started_at),
        millisecond) / 1000 as gap_before_seconds
from {{ ref('stg_capture_intervals') }}
