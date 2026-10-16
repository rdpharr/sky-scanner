-- One row per day, required by MetricFlow to aggregate metrics over time.
select day as date_day
from unnest(generate_date_array('2026-09-01', date_add(current_date(), interval 1 year))) as day
