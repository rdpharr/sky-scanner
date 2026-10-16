-- Leak test: no aircraft whose owner opted out of display (LADD) or that uses
-- a Privacy ICAO Address may appear anywhere in the public layer.
with hidden as (
    select address_type, address from {{ ref('dim_aircraft') }} where is_ladd or is_pia
)
select 'pub_aircraft' as model, address from {{ ref('pub_aircraft') }} inner join hidden using (address_type, address)
union all
select 'pub_sightings', address from {{ ref('pub_sightings') }} inner join hidden using (address_type, address)
union all
select distinct 'pub_snapshots', address from {{ ref('pub_snapshots') }} inner join hidden using (address_type, address)
union all
select distinct 'pub_live_snapshots', l.address
from {{ ref('pub_live_snapshots') }} as l
inner join {{ ref('int_privacy_hidden_addresses') }} as h using (address_type, address)
