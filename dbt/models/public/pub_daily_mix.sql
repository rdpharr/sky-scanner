-- Daily aircraft mix. Aggregates only; none identifies an aircraft.
select * from {{ ref('agg_daily_mix') }}
