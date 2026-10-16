select
    mode,
    started_at,
    duration_seconds,
    nodes,
    errors,
    warnings,
    bytes_billed,
    passed
from {{ ref('ops_dbt_runs') }}
