-- War Watch Intel callsign-family lookups (military callwords only).
select * from {{ ref('stg_callword_lookups') }}
