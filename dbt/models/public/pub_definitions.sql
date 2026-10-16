-- Canonical definitions: every metric, plus every public view and column.
select
    kind,
    name,
    model,
    label,
    description,
    metric_type,
    formula,
    filter,
    exported_at
from {{ source('ops', 'dbt_definitions') }}
where kind = 'metric' or dataset = 'public'
