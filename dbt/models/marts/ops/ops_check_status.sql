{{ config(materialized="view") }}
-- A view so the website sees each freshness run as soon as it lands.
-- Current state of every data check (tests and source freshness), from
-- scheduled runs over the last 7 days: latest result, when it last passed,
-- and how often it failed.
with results as (
    select *
    from {{ source('ops', 'dbt_node_results') }}
    where
        finished_at >= timestamp_sub(current_timestamp(), interval 7 day)
        and resource_type in ('test', 'source')
        and invocation_id in (select r.invocation_id from {{ ref('ops_dbt_runs') }} as r)
)

select
    unique_id,
    any_value(resource_type) as check_type,
    any_value(name) as check_name,
    array_agg(status order by finished_at desc limit 1)[offset(0)] as latest_status,
    array_agg(failures order by finished_at desc limit 1)[offset(0)] as latest_failures,
    array_agg(age_seconds order by finished_at desc limit 1)[offset(0)] as latest_age_seconds,
    max(finished_at) as last_checked_at,
    max(if(status = 'pass', finished_at, null)) as last_passed_at,
    count(*) as runs_7d,
    countif(status in ('fail', 'error', 'runtime error')) as failed_runs_7d,
    countif(status = 'warn') as warned_runs_7d
from results
group by unique_id
