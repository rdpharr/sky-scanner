-- One radio: a receiver's listening turns can never overlap.
select capture_interval_id, started_at, previous_ended_at
from (
    select capture_interval_id, started_at,
           lag(ended_at) over (partition by receiver_id order by started_at) as previous_ended_at
    from {{ ref('fct_capture_intervals') }}
)
where started_at < previous_ended_at
