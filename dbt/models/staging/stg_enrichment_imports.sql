select
    id as import_id,
    source_name,
    source_url,
    retrieved_at,
    imported_at,
    dataset_hash,
    record_count,
    changed_count
from {{ source('raw', 'enrichment_imports') }}
