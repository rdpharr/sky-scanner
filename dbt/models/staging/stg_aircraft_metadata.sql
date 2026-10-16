{{ config(tags=["daily"]) }}
-- plane-alert-db marks unknown values with runs of "?"; treat them as null.
{% set text_columns = ['registration', 'manufacturer', 'model', 'aircraft_type',
    'registered_owner', 'operator', 'icao_type_code', 'operator_icao', 'country',
    'category', 'tags', 'registrant_type'] %}

select
    id as metadata_id,
    address_type,
    upper(address) as address,
    source_name,
    registry_status,
    {% for column in text_columns -%}
        if(
            regexp_contains(trim({{ column }}), r'^\?*$'), null,
            regexp_replace(trim({{ column }}), r'\s+', ' ')
        ) as {{ column }},
    {% endfor -%}
    year_mfr,
    source_url,
    retrieved_at,
    dataset_hash,
    import_id
from {{ source('raw', 'aircraft_metadata') }}
