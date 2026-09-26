-- Recent observations (UTC session recommended).
SET TIME ZONE 'UTC';
SELECT bucket_start, address, frequency_hz, callsign, altitude_baro_ft,
       ground_speed_kt, latitude, longitude, message_count
FROM aircraft_snapshots
WHERE last_received_at > now() - interval '10 minutes'
ORDER BY bucket_start DESC LIMIT 50;

-- Received aircraft without a decoded position are retained.
SELECT address, frequency_hz, max(last_received_at) AS last_heard
FROM aircraft_snapshots WHERE position IS NULL
GROUP BY address, frequency_hz ORDER BY last_heard DESC LIMIT 20;

-- Band coverage and observations are distinct: intervals may contain zero messages.
SELECT frequency_hz, count(*) AS snapshots, sum(message_count) AS messages
FROM aircraft_snapshots GROUP BY frequency_hz;
SELECT frequency_hz, started_at, ended_at, sample_config
FROM capture_intervals ORDER BY started_at DESC LIMIT 10;

-- Substitute a real ICAO address to explore a track.
SELECT last_received_at, latitude, longitude, altitude_baro_ft, fields->'latitude'
FROM aircraft_snapshots WHERE address_type = 'icao' AND address = 'A4F107'
ORDER BY last_received_at;

-- Attribution is explicit; this view intentionally uses current registry data.
SELECT bucket_start, address, registration, manufacturer, model,
       registered_owner, operator, metadata_source_url, metadata_retrieved_at
FROM enriched_snapshots_current ORDER BY bucket_start DESC LIMIT 20;
