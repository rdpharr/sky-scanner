{%- set lookback_days = 2 -%}
{{ config(
    materialized='incremental',
    unique_key='snapshot_id',
    incremental_strategy='merge',
    partition_by={'field': 'bucket_start', 'data_type': 'timestamp', 'granularity': 'day'},
    cluster_by=['address'],
    on_schema_change='append_new_columns',
    incremental_predicates=[
        "DBT_INTERNAL_DEST.bucket_start >= timestamp_sub(current_timestamp(), interval "
        ~ (lookback_days + 1) ~ " day)"]
) }}

{#- Incremental runs read and merge only recent partitions. The cutoff is
    computed first so it reaches BigQuery as a literal, which (unlike a
    subquery) lets it skip older partitions. Rows arriving more than
    lookback_days late are picked up by the next full refresh. -#}
{% if is_incremental() %}
    {%- set cutoff = run_query(
        "select format_timestamp('%Y-%m-%d %H:%M:%S', timestamp_sub(max(bucket_start), interval "
        ~ lookback_days ~ " day)) from " ~ this).columns[0].values()[0] -%}
{% endif %}

select *
from {{ ref('stg_aircraft_snapshots') }}
{% if is_incremental() %}
where bucket_start >= timestamp '{{ cutoff }}'
{% endif %}
