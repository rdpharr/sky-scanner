select s.*
from {{ ref('fct_sightings') }} as s
inner join {{ ref('pub_aircraft') }} as a using (address_type, address)
