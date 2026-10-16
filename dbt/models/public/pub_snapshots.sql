-- Every 15-second snapshot of a public aircraft. Filter on bucket_start:
-- the underlying table is partitioned by day.
select
    s.snapshot_id,
    s.bucket_start,
    s.address_type,
    s.address,
    s.band_mhz,
    s.first_received_at,
    s.last_received_at,
    s.message_count,
    s.callsign,
    s.latitude,
    s.longitude,
    s.position,
    s.altitude_baro_ft,
    s.altitude_geom_ft,
    s.ground_speed_kt,
    s.track_deg,
    s.vertical_rate_baro_fpm,
    s.vertical_rate_geom_fpm,
    s.squawk,
    s.on_ground,
    s.fields
from {{ ref('fct_snapshots') }} as s
inner join {{ ref('pub_aircraft') }} as a using (address_type, address)
