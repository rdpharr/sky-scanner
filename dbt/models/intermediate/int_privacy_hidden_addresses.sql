{{ config(tags=["daily"]) }}
-- Every address that must never appear publicly, whether or not it has been
-- heard yet: tar1090-db's current LADD and PIA flags. Lets views over raw,
-- live data exclude aircraft that dim_aircraft has not seen.
select distinct address_type, address
from {{ ref('int_metadata_versions') }}
where source_name = 'tar1090_db'
  and is_latest
  and registry_status = 'present'
  and not 'miscode' in unnest(split(coalesce(tags, ''), ', '))
  and exists(select 1 from unnest(split(coalesce(tags, ''), ', ')) as tag
             where tag in ('LADD', 'privacy ICAO address (PIA)'))
