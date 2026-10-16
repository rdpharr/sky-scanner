-- Prototype (Postgres dialect, run against the edge database): per-sighting
-- path features used to design fct_notable_candidates_7d. See docs/PLAN.md §4.8.
CREATE TEMP TABLE wk AS SELECT now() - interval '7 days' AS since;

CREATE TEMP TABLE snaps AS
SELECT s.address_type, s.address, s.bucket_start, s.last_received_at, s.message_count,
       s.callsign, s.latitude, s.longitude, s.position, s.altitude_baro_ft,
       s.ground_speed_kt, s.track_deg, s.squawk, s.on_ground,
       CASE WHEN lag(s.bucket_start) OVER w IS NULL
              OR s.bucket_start - lag(s.bucket_start) OVER w > interval '10 minutes'
            THEN 1 ELSE 0 END AS new_sighting
FROM aircraft_snapshots s
WINDOW w AS (PARTITION BY s.address_type, s.address ORDER BY s.bucket_start);

CREATE TEMP TABLE snaps2 AS
SELECT *, sum(new_sighting) OVER (PARTITION BY address_type, address ORDER BY bucket_start) AS sighting_no
FROM snaps;

-- Path metrics per sighting from positioned snapshots.
CREATE TEMP TABLE pts AS
SELECT address_type, address, sighting_no, bucket_start, position, altitude_baro_ft, on_ground,
       track_deg,
       ST_Distance(position, lag(position) OVER p) / 1852.0 AS seg_nm,
       bucket_start - lag(bucket_start) OVER p AS seg_dt,
       lag(track_deg) OVER p AS prev_track,
       -- distance to nearest local airport (nm): LAS, VGT, HND, LSV, BVU, INS
       least(
         ST_Distance(position, ST_MakePoint(-115.1523, 36.0840)::geography),
         ST_Distance(position, ST_MakePoint(-115.1946, 36.2107)::geography),
         ST_Distance(position, ST_MakePoint(-115.1344, 35.9728)::geography),
         ST_Distance(position, ST_MakePoint(-115.0342, 36.2362)::geography),
         ST_Distance(position, ST_MakePoint(-114.8638, 35.9474)::geography),
         ST_Distance(position, ST_MakePoint(-115.6734, 36.5872)::geography)
       ) / 1852.0 AS airport_nm
FROM snaps2
WHERE position IS NOT NULL
WINDOW p AS (PARTITION BY address_type, address, sighting_no ORDER BY bucket_start);

CREATE TEMP TABLE sightings AS
SELECT s.address_type, s.address, s.sighting_no,
       min(s.bucket_start) AS started, max(s.last_received_at) AS ended,
       count(*) AS snaps, sum(s.message_count) AS msgs,
       bool_or(s.squawk IN ('7500','7600','7700')) AS emergency,
       max(s.altitude_baro_ft) AS max_alt, min(s.altitude_baro_ft) FILTER (WHERE NOT coalesce(s.on_ground,false)) AS min_alt,
       max(s.ground_speed_kt) AS max_gs,
       (SELECT coalesce(sum(seg_nm) FILTER (WHERE seg_dt <= interval '2 minutes'), 0) FROM pts p
         WHERE p.address_type = s.address_type AND p.address = s.address AND p.sighting_no = s.sighting_no) AS path_nm,
       (SELECT ST_Distance(
                 (array_agg(position ORDER BY bucket_start))[1]::geography,
                 (array_agg(position ORDER BY bucket_start DESC))[1]::geography) / 1852.0 FROM pts p
         WHERE p.address_type = s.address_type AND p.address = s.address AND p.sighting_no = s.sighting_no) AS disp_nm,
       (SELECT coalesce(sum(abs(((track_deg - prev_track + 540)::numeric % 360) - 180))
                        FILTER (WHERE seg_dt <= interval '1 minute'), 0) FROM pts p
         WHERE p.address_type = s.address_type AND p.address = s.address AND p.sighting_no = s.sighting_no) AS turn_deg,
       (SELECT count(*) FROM pts p
         WHERE p.address_type = s.address_type AND p.address = s.address AND p.sighting_no = s.sighting_no
           AND p.altitude_baro_ft < 4500 AND NOT coalesce(p.on_ground,false) AND p.airport_nm > 5) AS low_away_snaps,
       (SELECT count(*) FROM pts p
         WHERE p.address_type = s.address_type AND p.address = s.address AND p.sighting_no = s.sighting_no) AS pos_snaps
FROM snaps2 s, wk
WHERE s.bucket_start >= wk.since
GROUP BY 1,2,3;
