select s.*
from {{ ref('fct_sightings') }} as s
-- Only aircraft in the public layer.
inner join {{ ref('pub_aircraft') }} using (address_type, address)
