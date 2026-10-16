-- Leak test: public owner names must never name a private owner.
-- 1. An aircraft the FAA classifies as individual, partnership or co-owned
--    shows no owner name, whatever source supplied one.
-- 2. A person's name that only ever appears on private FAA registrations
--    (never a corporation, government, LLC or non-citizen corporation) is never shown,
--    even for another aircraft or from another source. Names that appear both
--    ways are organizations (e.g. fractional-ownership programs registering
--    co-owned aircraft under the company name).
-- 3. Names mentioning a trust or trustee are never shown.
with registrations as (
    select
        upper(registered_owner) as name,
        registrant_type
    from {{ ref('stg_aircraft_metadata') }}
    where
        source_name = 'faa_registry' and registered_owner is not null
        and registrant_type is not null
),

private_only_names as (
    -- Company names are exempt: on a co-owned registration the name field holds
    -- the first co-owner, which can be an LLC. The rule targets people's names.
    select name
    from registrations
    where
        not regexp_contains(
            name, r'\b(LLC|L L C|INC|CORP|CORPORATION|COMPANY|CO|LTD|LP|LLP|PLC|GMBH|AIRLINES?|AVIATION)\b'
        )
    group by name
    having logical_and(registrant_type in ('1', '2', '4', '9'))
)

select
    p.address,
    p.owner_name,
    a.faa_registrant_type,
    'aircraft registered privately' as reason
from {{ ref('pub_aircraft') }} as p
inner join {{ ref('dim_aircraft') }} as a using (address_type, address)
where p.owner_name is not null and a.faa_registrant_type in ('1', '2', '4', '9')

union all

select
    address,
    owner_name,
    null as faa_registrant_type,
    'name only ever registered privately' as reason
from {{ ref('pub_aircraft') }}
where upper(owner_name) in (select n.name from private_only_names as n)

union all

select
    address,
    owner_name,
    null as faa_registrant_type,
    'trust' as reason
from {{ ref('pub_aircraft') }}
where regexp_contains(upper(owner_name), r'\bTRUST(EE)?S?\b')
