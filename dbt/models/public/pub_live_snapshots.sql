-- Snapshots from the last 30 minutes, read straight from raw so the live map
-- is seconds old rather than waiting for the hourly build. Hidden (LADD/PIA)
-- addresses are excluded even if the aircraft is new. Identity columns are
-- null for aircraft first heard since the last build.
with recent as (
    select *
    from {{ source('raw', 'aircraft_snapshots') }}
    where bucket_start >= timestamp_sub(current_timestamp(), interval 30 minute)
    qualify row_number() over (
        partition by bucket_start, receiver_id, address_type, address, frequency_hz
        order by written_at desc, _replicated_at desc) = 1
)

select
    r.bucket_start,
    r.address_type,
    upper(r.address) as address,
    div(r.frequency_hz, 1000000) as band_mhz,
    r.last_received_at,
    r.message_count,
    nullif(trim(r.callsign), '') as callsign,
    r.latitude,
    r.longitude,
    r.altitude_baro_ft,
    r.ground_speed_kt,
    r.track_deg,
    r.vertical_rate_baro_fpm,
    r.squawk,
    r.on_ground,
    a.registration,
    a.designator,
    a.type_name,
    a.aircraft_class,
    a.aircraft_kind,
    a.operator,
    a.is_military
from recent as r
left join {{ ref('pub_aircraft') }} as a
    on a.address_type = r.address_type and a.address = upper(r.address)
where not exists (
    select 1 from {{ ref('int_privacy_hidden_addresses') }} as h
    where h.address_type = r.address_type and h.address = upper(r.address))
