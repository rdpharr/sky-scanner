-- Leak test: the public dataset has no columns that carry raw owner names or
-- receiver location.
select
    table_name,
    column_name
-- BigQuery names INFORMATION_SCHEMA views in upper case.
from `{{ target.project }}`.`{{ generate_schema_name('public', none) }}`.INFORMATION_SCHEMA.COLUMNS  -- noqa: CP02, LT05
where lower(
    column_name) in (
    'owner', 'registered_owner', 'faa_owner', 'tar1090_owner',
    'opensky_owner', 'location', 'receiver_location'
)
