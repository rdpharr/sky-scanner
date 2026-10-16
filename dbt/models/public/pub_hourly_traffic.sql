-- Hourly totals with receiver uptime. Aggregates only; none identifies an aircraft.
select * from {{ ref('agg_hourly_traffic') }}
