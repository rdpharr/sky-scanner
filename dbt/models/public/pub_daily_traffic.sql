-- Daily totals per band. Aggregates include every aircraft; none identifies one.
select * from {{ ref('agg_daily_traffic') }}
