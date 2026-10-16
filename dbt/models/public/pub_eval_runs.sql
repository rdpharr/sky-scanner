-- Agent evaluations for the website: Q&A accuracy per model and edition
-- recall against the owner's hand-checked reference week.
select
    run_id,
    kind,
    model,
    started_at,
    score,
    passed,
    total,
    cost_usd,
    seconds,
    details
from {{ source('ai', 'eval_runs') }}
