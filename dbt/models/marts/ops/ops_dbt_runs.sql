{{ config(materialized="view") }}
-- Scheduled dbt runs, newest first in practice. Local runs are excluded.
select
    invocation_id,
    mode,
    started_at,
    finished_at,
    timestamp_diff(finished_at, started_at, second) as duration_seconds,
    dbt_exit_code,
    nodes,
    errors,
    warnings,
    bytes_billed,
    errors = 0 as passed
from {{ source('ops', 'dbt_invocations') }}
where execution_environment != 'local'
