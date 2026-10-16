{{ config(
    materialized='incremental',
    unique_key='snapshot_id',
    incremental_strategy='merge',
    partition_by={'field': 'bucket_start', 'data_type': 'timestamp', 'granularity': 'day'},
    cluster_by=['address'],
    on_schema_change='append_new_columns'
) }}

select *
from {{ ref('stg_aircraft_snapshots') }}
{% if is_incremental() %}
-- Re-read an hour of overlap so rows replicated during the last build are included.
where _replicated_at > (select timestamp_sub(max(_replicated_at), interval 1 hour) from {{ this }})
{% endif %}
