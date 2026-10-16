{{ config(tags=["daily"]) }}
-- Rebuilt daily (the metadata it reads reloads once a day); 900k+ rows.
-- Every identity version per source, with the period it was current.
select
    *,
    retrieved_at as valid_from,
    lead(retrieved_at) over (
        partition by source_name, address_type, address
        order by retrieved_at, metadata_id) as valid_to,
    row_number() over (
        partition by source_name, address_type, address
        order by retrieved_at desc, metadata_id desc) = 1 as is_latest
from {{ ref('stg_aircraft_metadata') }}
