select
    hour_start,
    listening_seconds_1090,
    listening_seconds_978,
    listening_seconds,
    uptime_ratio
from {{ ref('fct_receiver_uptime_hourly') }}
