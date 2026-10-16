-- One row per snapshot key; replication can deliver a key more than once.
with ranked as (
    select *
    from {{ source('raw', 'aircraft_snapshots') }}
    qualify row_number() over (
        partition by bucket_start, receiver_id, address_type, address, frequency_hz
        order by written_at desc, _replicated_at desc
    ) = 1
)

select
    {{ dbt_utils.generate_surrogate_key(
        ['bucket_start', 'receiver_id', 'address_type', 'address', 'frequency_hz']) }} as snapshot_id,
    bucket_start,
    receiver_id,
    address_type,
    upper(address) as address,
    frequency_hz,
    div(frequency_hz, 1000000) as band_mhz,
    first_received_at,
    last_received_at,
    message_count,
    nullif(trim(callsign), '') as callsign,
    latitude,
    longitude,
    if(
        latitude is not null and longitude is not null,
        st_geogpoint(longitude, latitude), null
    ) as position,
    altitude_baro_ft,
    altitude_geom_ft,
    ground_speed_kt,
    track_deg,
    vertical_rate_baro_fpm,
    vertical_rate_geom_fpm,
    squawk,
    on_ground,
    fields,
    written_at,
    _replicated_at
from ranked
