-- A sighting is a run of an aircraft's snapshots, across both bands, with no
-- gap longer than var('sighting_gap_minutes') (10). Path features describe
-- the run's positioned snapshots.
with snapshots as (
    select
        *,
        if(
            lag(bucket_start) over w is null
            or timestamp_diff(bucket_start, lag(bucket_start) over w, second)
            > {{ var('sighting_gap_minutes') }} * 60, 1, 0
        ) as starts_sighting
    from {{ ref('fct_snapshots') }}
    window w as (partition by address_type, address order by bucket_start, frequency_hz)
),

numbered as (
    select
        *,
        sum(starts_sighting) over (
            partition by address_type, address order by bucket_start, frequency_hz
            rows between unbounded preceding and current row
        ) as sighting_number
    from snapshots
),

positioned as (
    select
        n.address_type,
        n.address,
        n.sighting_number,
        n.bucket_start,
        n.position,
        n.altitude_baro_ft,
        n.on_ground,
        n.track_deg,
        st_distance(n.position, lag(n.position) over p) / 1852 as segment_nm,
        timestamp_diff(n.bucket_start, lag(n.bucket_start) over p, second) as segment_seconds,
        lag(n.track_deg) over p as previous_track_deg,
        (
            select min(st_distance(n.position, st_geogpoint(a.longitude, a.latitude))) / 1852
            from {{ ref('local_airports') }} as a
        ) as nearest_airport_nm
    from numbered as n
    where n.position is not null
    window p as (partition by n.address_type, n.address, n.sighting_number order by n.bucket_start)
),

paths as (
    select
        address_type,
        address,
        sighting_number,
        count(*) as positioned_snapshots,
        -- Segments longer than 2 minutes are gaps, not flown distance.
        sum(if(segment_seconds <= 120, segment_nm, 0)) as path_nm,
        st_distance(
            array_agg(position order by bucket_start limit 1)[offset(0)],
            array_agg(position order by bucket_start desc limit 1)[offset(0)]) / 1852
            as displacement_nm,
        -- Heading change between snapshots at most a minute apart.
        sum(if(
            segment_seconds <= 60 and track_deg is not null and previous_track_deg is not null,
            abs(mod(cast(track_deg - previous_track_deg + 540 as numeric), 360) - 180), 0
        ))
            as total_turn_deg,
        countif(
            altitude_baro_ft < 4500 and not coalesce(on_ground, false)
            and nearest_airport_nm > 5
        ) as low_away_from_airport_snapshots,
        min(nearest_airport_nm) as closest_airport_nm
    from positioned
    group by address_type, address, sighting_number
),

sightings as (
    select
        address_type,
        address,
        sighting_number,
        min(first_received_at) as started_at,
        max(last_received_at) as ended_at,
        count(*) as snapshots,
        sum(message_count) as messages,
        array_agg(distinct band_mhz order by band_mhz) as bands_mhz,
        array_agg(distinct callsign ignore nulls order by callsign) as callsigns,
        array_agg(distinct squawk ignore nulls order by squawk) as squawks,
        min(altitude_baro_ft) as min_altitude_baro_ft,
        max(altitude_baro_ft) as max_altitude_baro_ft,
        max(ground_speed_kt) as max_ground_speed_kt,
        logical_or(coalesce(on_ground, false)) as seen_on_ground,
        logical_or(squawk in ('7500', '7600', '7700')) as emergency_squawk
    from numbered
    group by address_type, address, sighting_number
)

select
    {{ dbt_utils.generate_surrogate_key(['s.address_type', 's.address', 's.sighting_number']) }}
        as sighting_id,
    s.*,
    timestamp_diff(s.ended_at, s.started_at, second) / 60 as duration_minutes,
    coalesce(p.positioned_snapshots, 0) as positioned_snapshots,
    p.path_nm,
    p.displacement_nm,
    -- Flown distance over straight-line distance; high values mean circling.
    if(p.displacement_nm > 0.5, p.path_nm / p.displacement_nm, null) as circling_ratio,
    p.total_turn_deg,
    p.low_away_from_airport_snapshots,
    p.closest_airport_nm,
    d.is_military,
    d.is_confirmed
from sightings as s
left join paths as p using (address_type, address, sighting_number)
left join {{ ref('dim_aircraft') }} as d using (address_type, address)
