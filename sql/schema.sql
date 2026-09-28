CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS receivers (
    receiver_id text PRIMARY KEY,
    description text,
    location geography(Point, 4326),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS capture_intervals (
    id uuid PRIMARY KEY,
    receiver_id text NOT NULL REFERENCES receivers(receiver_id),
    frequency_hz bigint NOT NULL,
    started_at timestamptz NOT NULL,
    ended_at timestamptz,
    decoder_version text,
    sample_config jsonb NOT NULL DEFAULT '{}'::jsonb,
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);
CREATE INDEX IF NOT EXISTS capture_intervals_receiver_start_idx
    ON capture_intervals (receiver_id, started_at DESC);

CREATE TABLE IF NOT EXISTS aircraft_snapshots (
    bucket_start timestamptz NOT NULL,
    receiver_id text NOT NULL REFERENCES receivers(receiver_id),
    address_type text NOT NULL,
    address text NOT NULL,
    frequency_hz bigint NOT NULL,
    first_received_at timestamptz NOT NULL,
    last_received_at timestamptz NOT NULL,
    message_count integer NOT NULL CHECK (message_count > 0),
    callsign text,
    latitude double precision,
    longitude double precision,
    position geography(Point, 4326) GENERATED ALWAYS AS (
        CASE WHEN latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180
        THEN ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography END
    ) STORED,
    altitude_baro_ft double precision,
    altitude_geom_ft double precision,
    ground_speed_kt double precision,
    track_deg double precision,
    vertical_rate_baro_fpm double precision,
    vertical_rate_geom_fpm double precision,
    squawk text,
    on_ground boolean,
    fields jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (bucket_start, receiver_id, address_type, address, frequency_hz),
    CHECK (last_received_at >= first_received_at),
    CHECK ((latitude IS NULL AND longitude IS NULL) OR
           (latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180))
);
CREATE INDEX IF NOT EXISTS aircraft_snapshots_aircraft_time_idx
    ON aircraft_snapshots (address_type, address, bucket_start DESC);
CREATE INDEX IF NOT EXISTS aircraft_snapshots_frequency_time_idx
    ON aircraft_snapshots (frequency_hz, bucket_start DESC);
CREATE INDEX IF NOT EXISTS aircraft_snapshots_position_gix
    ON aircraft_snapshots USING gist (position);

CREATE TABLE IF NOT EXISTS enrichment_imports (
    id uuid PRIMARY KEY,
    source_url text NOT NULL,
    retrieved_at timestamptz NOT NULL,
    dataset_hash text NOT NULL,
    record_count integer NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_url, dataset_hash)
);

CREATE TABLE IF NOT EXISTS aircraft_metadata (
    id bigserial PRIMARY KEY,
    address_type text NOT NULL DEFAULT 'icao',
    address text NOT NULL,
    registration text,
    manufacturer text,
    model text,
    aircraft_type text,
    registered_owner text,
    operator text,
    source_url text NOT NULL,
    retrieved_at timestamptz NOT NULL,
    dataset_hash text NOT NULL,
    import_id uuid REFERENCES enrichment_imports(id),
    CONSTRAINT aircraft_metadata_source_hash_key
        UNIQUE (address_type, address, source_url, dataset_hash)
);
ALTER TABLE aircraft_metadata DROP CONSTRAINT IF EXISTS
    aircraft_metadata_address_type_address_source_url_retrieved_at_dataset_hash_key;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'aircraft_metadata'::regclass
          AND conname = 'aircraft_metadata_source_hash_key'
    ) THEN
        ALTER TABLE aircraft_metadata ADD CONSTRAINT
            aircraft_metadata_source_hash_key
            UNIQUE (address_type, address, source_url, dataset_hash);
    END IF;
END $$;
-- Versions are written only on change; 'absent' marks removal from the source.
ALTER TABLE aircraft_metadata ADD COLUMN IF NOT EXISTS registry_status text
    NOT NULL DEFAULT 'present' CHECK (registry_status IN ('present', 'absent'));
ALTER TABLE enrichment_imports ADD COLUMN IF NOT EXISTS changed_count integer;
-- Metadata sources are kept apart: 'faa_registry' (official) and 'opensky'
-- (crowdsourced, unverified). Views never let one source replace the other.
ALTER TABLE enrichment_imports ADD COLUMN IF NOT EXISTS source_name text
    NOT NULL DEFAULT 'faa_registry';
ALTER TABLE aircraft_metadata ADD COLUMN IF NOT EXISTS source_name text
    NOT NULL DEFAULT 'faa_registry';
ALTER TABLE aircraft_metadata ADD COLUMN IF NOT EXISTS icao_type_code text;
ALTER TABLE aircraft_metadata ADD COLUMN IF NOT EXISTS operator_icao text;
ALTER TABLE aircraft_metadata ADD COLUMN IF NOT EXISTS country text;
ALTER TABLE aircraft_metadata ADD COLUMN IF NOT EXISTS category text;
ALTER TABLE aircraft_metadata ADD COLUMN IF NOT EXISTS tags text;
CREATE INDEX IF NOT EXISTS aircraft_metadata_source_lookup_idx
    ON aircraft_metadata (source_name, address_type, address, retrieved_at DESC);
CREATE INDEX IF NOT EXISTS aircraft_metadata_lookup_idx
    ON aircraft_metadata (address_type, address, retrieved_at DESC);

CREATE OR REPLACE VIEW latest_aircraft AS
SELECT DISTINCT ON (receiver_id, address_type, address, frequency_hz)
       *
FROM aircraft_snapshots
ORDER BY receiver_id, address_type, address, frequency_hz, bucket_start DESC;

CREATE OR REPLACE VIEW enriched_snapshots AS
SELECT s.*, m.registration, m.manufacturer, m.model, m.aircraft_type,
       m.registered_owner, m.operator, m.source_url AS metadata_source_url,
       m.retrieved_at AS metadata_retrieved_at, m.dataset_hash AS metadata_dataset_hash,
       m.registry_status,
       o.registration AS opensky_registration, o.manufacturer AS opensky_manufacturer,
       o.model AS opensky_model, o.icao_type_code AS opensky_type_code,
       o.operator AS opensky_operator, o.operator_icao AS opensky_operator_icao,
       o.registered_owner AS opensky_owner, o.country AS opensky_country,
       o.source_url AS opensky_source_url, o.retrieved_at AS opensky_retrieved_at
FROM aircraft_snapshots AS s
LEFT JOIN LATERAL (
    SELECT metadata.*
    FROM aircraft_metadata AS metadata
    WHERE metadata.source_name = 'faa_registry'
      AND metadata.address_type = s.address_type
      AND metadata.address = s.address
      AND metadata.retrieved_at <= s.bucket_start + interval '15 seconds'
    ORDER BY metadata.retrieved_at DESC, metadata.id DESC
    LIMIT 1
) AS m ON true
LEFT JOIN LATERAL (
    SELECT o.*
    FROM aircraft_metadata AS o
    WHERE o.source_name = 'opensky' AND o.registry_status = 'present'
      AND o.address_type = s.address_type
      AND o.address = s.address
      AND o.retrieved_at <= s.bucket_start + interval '15 seconds'
    ORDER BY o.retrieved_at DESC, o.id DESC
    LIMIT 1
) AS o ON true;

-- Explicitly current enrichment for users exploring older observations.
-- Unlike enriched_snapshots, this intentionally may show later metadata.
CREATE OR REPLACE VIEW enriched_snapshots_current AS
SELECT s.*, m.registration, m.manufacturer, m.model, m.aircraft_type,
       m.registered_owner, m.operator, m.source_url AS metadata_source_url,
       m.retrieved_at AS metadata_retrieved_at, m.dataset_hash AS metadata_dataset_hash,
       m.registry_status,
       o.registration AS opensky_registration, o.manufacturer AS opensky_manufacturer,
       o.model AS opensky_model, o.icao_type_code AS opensky_type_code,
       o.operator AS opensky_operator, o.operator_icao AS opensky_operator_icao,
       o.registered_owner AS opensky_owner, o.country AS opensky_country,
       o.source_url AS opensky_source_url, o.retrieved_at AS opensky_retrieved_at
FROM aircraft_snapshots AS s
LEFT JOIN LATERAL (
    SELECT metadata.*
    FROM aircraft_metadata AS metadata
    WHERE metadata.source_name = 'faa_registry'
      AND metadata.address_type = s.address_type
      AND metadata.address = s.address
    ORDER BY metadata.retrieved_at DESC, metadata.id DESC
    LIMIT 1
) AS m ON true
LEFT JOIN LATERAL (
    SELECT o.*
    FROM aircraft_metadata AS o
    WHERE o.source_name = 'opensky' AND o.registry_status = 'present'
      AND o.address_type = s.address_type
      AND o.address = s.address
    ORDER BY o.retrieved_at DESC, o.id DESC
    LIMIT 1
) AS o ON true;

-- Military identification (military aircraft only). Current reference set of
-- military ICAO operator codes; provenance is recorded in enrichment_imports.
CREATE TABLE IF NOT EXISTS military_operator_codes (
    code text PRIMARY KEY CHECK (code ~ '^[A-Z]{3}$'),
    name text NOT NULL,
    import_id uuid REFERENCES enrichment_imports(id)
);

-- Cached War Watch Intel callsign-family lookups for callwords used by
-- aircraft already identified as military.
CREATE TABLE IF NOT EXISTS callword_lookups (
    word text PRIMARY KEY CHECK (word ~ '^[A-Z]{3,}$'),
    status text NOT NULL CHECK (status IN ('found', 'not_found')),
    source_url text NOT NULL,
    fetched_at timestamptz NOT NULL,
    data jsonb
);

-- One representative Wikipedia article/photo per ICAO aircraft type designator.
-- The image is a hotlink to a Wikimedia Commons file, not stored here.
CREATE TABLE IF NOT EXISTS aircraft_type_images (
    designator text PRIMARY KEY CHECK (designator ~ '^[A-Z0-9]{2,4}$'),
    model_name text,
    status text NOT NULL CHECK (status IN ('found', 'not_found')),
    method text,
    title text,
    description text,
    article_url text,
    image_url text,
    file_url text,
    fetched_at timestamptz NOT NULL
);

-- Default privileges only cover objects created after they were set; grant
-- explicitly so the read-only exploration role always sees every table/view.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'explorer') THEN
        GRANT USAGE ON SCHEMA public TO explorer;
        GRANT SELECT ON ALL TABLES IN SCHEMA public TO explorer;
    END IF;
END $$;
