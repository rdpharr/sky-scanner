-- Leak test: no public owner name may belong to an FAA registrant that is an
-- individual, partnership or co-owned (any version, any time), or mention a trust.
with private_names as (
    select distinct upper(registered_owner) as name
    from {{ ref('stg_aircraft_metadata') }}
    where source_name = 'faa_registry' and registrant_type in ('1', '2', '4', '9')
)
select address, owner_name
from {{ ref('pub_aircraft') }}
where upper(owner_name) in (select name from private_names)
   or regexp_contains(upper(owner_name), r'\bTRUST(EE)?S?\b')
