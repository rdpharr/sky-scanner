-- Data checks for the website's Data health page.
select
    check_type,
    check_name,
    latest_status,
    latest_failures,
    latest_age_seconds,
    last_checked_at,
    last_passed_at,
    runs_7d,
    failed_runs_7d,
    warned_runs_7d
from {{ ref('ops_check_status') }}
