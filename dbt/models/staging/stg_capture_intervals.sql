-- Intervals are written when a turn starts and again when it ends; keep the latest.
select
    id as capture_interval_id,
    receiver_id,
    frequency_hz,
    div(frequency_hz, 1000000) as band_mhz,
    started_at,
    ended_at,
    timestamp_diff(ended_at, started_at, millisecond) / 1000 as listening_seconds,
    decoder_version,
    sample_config,
    written_at,
    _replicated_at
from {{ source('raw', 'capture_intervals') }}
qualify row_number() over (partition by id order by written_at desc, _replicated_at desc) = 1
