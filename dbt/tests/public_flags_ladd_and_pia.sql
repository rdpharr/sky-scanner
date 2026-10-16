-- Since 2026-10-16 LADD and PIA aircraft are public but must be flagged, so
-- every page and agent can say so: the public flags must match the identity
-- resolution for every aircraft.
select
    d.address,
    d.is_ladd,
    p.is_ladd as public_is_ladd,
    d.is_pia,
    p.is_pia as public_is_pia
from {{ ref('dim_aircraft') }} as d
left join {{ ref('pub_aircraft') }} as p using (address_type, address)
where
    p.address is null
    or p.is_ladd is distinct from d.is_ladd
    or p.is_pia is distinct from d.is_pia
