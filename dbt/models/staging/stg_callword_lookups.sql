select
    word as callword,
    status as lookup_status,
    source_url,
    fetched_at,
    data as lookup_data
from {{ source('raw', 'callword_lookups') }}
