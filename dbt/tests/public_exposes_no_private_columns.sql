-- Leak test: the public dataset has no columns that carry raw owner names or
-- receiver location.
select table_name, column_name
from `{{ target.project }}`.`{{ generate_schema_name('public', none) }}`.INFORMATION_SCHEMA.COLUMNS
where lower(column_name) in ('owner', 'registered_owner', 'faa_owner', 'tar1090_owner',
                             'opensky_owner', 'location', 'receiver_location')
