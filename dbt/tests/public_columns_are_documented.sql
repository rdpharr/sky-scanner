-- Agents read column descriptions straight from BigQuery, so every column of
-- every public view must carry one (persist_docs writes them on build).
-- Runs after the public views it checks:
{% for m in ['pub_aircraft', 'pub_snapshots', 'pub_live_snapshots', 'pub_sightings',
             'pub_aircraft_types', 'pub_daily_traffic', 'pub_receiver_uptime_hourly',
             'pub_callword_lookups', 'pub_data_health', 'pub_dbt_runs', 'pub_definitions'] -%}
-- depends_on: {{ ref(m) }}
{% endfor %}
select table_name, column_name
from `{{ target.project }}`.`{{ generate_schema_name('public', none) }}`.INFORMATION_SCHEMA.COLUMN_FIELD_PATHS
where column_name = field_path
  and coalesce(trim(description), '') = ''
