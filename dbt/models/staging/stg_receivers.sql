select
    receiver_id,
    description,
    created_at
from {{ source('raw', 'receivers') }}
