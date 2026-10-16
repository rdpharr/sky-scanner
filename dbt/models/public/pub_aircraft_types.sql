-- Aircraft types heard, counted over public aircraft only.
select
    t.designator,
    t.type_name,
    t.aircraft_class,
    t.doc8643_description,
    t.wake_category,
    t.engine_type,
    t.engine_count,
    countif(a.is_confirmed) as aircraft_heard,
    countif(a.is_confirmed and a.is_military) as military_aircraft_heard,
    min(a.first_seen_at) as first_seen_at,
    max(a.last_seen_at) as last_seen_at,
    t.wikipedia_article_url,
    t.wikipedia_title,
    t.wikipedia_description,
    t.image_url,
    t.image_file_url
from {{ ref('dim_aircraft_type') }} as t
inner join {{ ref('pub_aircraft') }} as a using (designator)
group by
    t.designator, t.type_name, t.aircraft_class, t.doc8643_description, t.wake_category,
    t.engine_type, t.engine_count, t.wikipedia_article_url, t.wikipedia_title,
    t.wikipedia_description, t.image_url, t.image_file_url
