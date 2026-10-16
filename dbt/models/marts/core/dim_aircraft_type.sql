-- One row per ICAO type designator heard here.
select
    a.designator,
    any_value(a.type_name having max a.last_seen_at) as type_name,
    any_value(a.aircraft_class) as aircraft_class,
    any_value(t.description) as doc8643_description,
    any_value(t.wake_category) as wake_category,
    any_value(t.engine_type) as engine_type,
    any_value(t.engine_count) as engine_count,
    countif(a.is_confirmed) as aircraft_heard,
    countif(a.is_confirmed and a.is_military) as military_aircraft_heard,
    min(a.first_seen_at) as first_seen_at,
    max(a.last_seen_at) as last_seen_at,
    any_value(img.article_url) as wikipedia_article_url,
    any_value(img.article_title) as wikipedia_title,
    any_value(img.article_description) as wikipedia_description,
    any_value(img.image_url) as image_url,
    any_value(img.file_url) as image_file_url
from {{ ref('dim_aircraft') }} as a
left join {{ ref('stg_doc8643_types') }} as t on t.designator = a.designator
left join {{ ref('stg_aircraft_type_images') }} as img
    on img.designator = a.designator and img.lookup_status = 'found'
where a.designator is not null
group by a.designator
