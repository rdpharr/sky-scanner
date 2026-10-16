select designator, model_name, status as lookup_status, method as lookup_method,
       title as article_title, description as article_description,
       article_url, image_url, file_url, fetched_at
from {{ source('raw', 'aircraft_type_images') }}
