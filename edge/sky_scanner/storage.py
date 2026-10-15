"""PostgreSQL persistence for snapshots and FAA enrichment.

The database connection may be injected (for example, a DB-API mock). Without
one, ``from_env`` imports psycopg 3 lazily and reads
``SKY_SCANNER_DATABASE_URL``. Methods commit each operation atomically and
roll back on errors. Metadata records follow ``enrichment.parse_faa_registry``.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


_SNAPSHOT_FIELDS = {
    "callsign": "callsign",
    "latitude": "latitude",
    "longitude": "longitude",
    "altitude_baro_ft": "altitude_baro_ft",
    "altitude_geom_ft": "altitude_geom_ft",
    "ground_speed_kt": "ground_speed_kt",
    "track_deg": "track_deg",
    "vertical_rate_baro_fpm": "vertical_rate_baro_fpm",
    "vertical_rate_geom_fpm": "vertical_rate_geom_fpm",
    "squawk": "squawk",
    "on_ground": "on_ground",
}

_SNAPSHOT_INSERT = """
INSERT INTO aircraft_snapshots (
 bucket_start, receiver_id, address_type, address, frequency_hz,
 first_received_at, last_received_at, message_count, callsign, latitude,
 longitude, altitude_baro_ft, altitude_geom_ft, ground_speed_kt, track_deg,
 vertical_rate_baro_fpm, vertical_rate_geom_fpm, squawk, on_ground, fields
) VALUES (
 %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb
)
ON CONFLICT (bucket_start, receiver_id, address_type, address, frequency_hz)
DO NOTHING
"""


_METADATA_COLUMNS = ("address_type, address, registration, manufacturer, model, "
                     "aircraft_type, registered_owner, operator, icao_type_code, "
                     "operator_icao, country, category, tags")

_METADATA_STAGE_CREATE = """
CREATE TEMP TABLE sky_metadata_stage (
 address_type text NOT NULL, address text NOT NULL, registration text,
 manufacturer text, model text, aircraft_type text, registered_owner text,
 operator text, icao_type_code text, operator_icao text, country text,
 category text, tags text
) ON COMMIT DROP
"""

# New or changed aircraft relative to the latest version from the same source.
_METADATA_INSERT_CHANGED = """
INSERT INTO aircraft_metadata
 (address_type, address, registration, manufacturer, model, aircraft_type,
  registered_owner, operator, icao_type_code, operator_icao, country, category, tags,
  registry_status, source_name, source_url, retrieved_at, dataset_hash, import_id)
SELECT s.address_type, s.address, s.registration, s.manufacturer, s.model,
       s.aircraft_type, s.registered_owner, s.operator, s.icao_type_code,
       s.operator_icao, s.country, s.category, s.tags, 'present', %(source_name)s,
       %(source_url)s, %(retrieved_at)s, %(dataset_hash)s, %(import_id)s
FROM (SELECT DISTINCT ON (address_type, address) * FROM sky_metadata_stage
      ORDER BY address_type, address) AS s
LEFT JOIN LATERAL (
    SELECT * FROM aircraft_metadata AS m
    WHERE m.address_type = s.address_type AND m.address = s.address
      AND m.source_name = %(source_name)s
    ORDER BY m.retrieved_at DESC, m.id DESC LIMIT 1
) AS latest ON true
WHERE latest.id IS NULL OR latest.registry_status <> 'present'
   OR (latest.registration, latest.manufacturer, latest.model, latest.aircraft_type,
       latest.registered_owner, latest.operator, latest.icao_type_code,
       latest.operator_icao, latest.country, latest.category, latest.tags)
      IS DISTINCT FROM
      (s.registration, s.manufacturer, s.model, s.aircraft_type,
       s.registered_owner, s.operator, s.icao_type_code, s.operator_icao, s.country,
       s.category, s.tags)
ON CONFLICT (address_type, address, source_url, dataset_hash) DO NOTHING
"""

# Previously present aircraft that this dataset no longer lists.
_METADATA_INSERT_ABSENT = """
INSERT INTO aircraft_metadata
 (address_type, address, registry_status, source_name, source_url, retrieved_at,
  dataset_hash, import_id)
SELECT latest.address_type, latest.address, 'absent', %(source_name)s,
       %(source_url)s, %(retrieved_at)s, %(dataset_hash)s, %(import_id)s
FROM (SELECT DISTINCT ON (address_type, address) address_type, address, registry_status
      FROM aircraft_metadata WHERE source_name = %(source_name)s
      ORDER BY address_type, address, retrieved_at DESC, id DESC) AS latest
WHERE latest.registry_status = 'present'
  AND NOT EXISTS (SELECT 1 FROM sky_metadata_stage AS s
                  WHERE s.address_type = latest.address_type AND s.address = latest.address)
ON CONFLICT (address_type, address, source_url, dataset_hash) DO NOTHING
"""


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromtimestamp(float(value), tz=timezone.utc)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _field_value(fields: dict[str, Any], name: str) -> Any:
    item = fields.get(name)
    return item.get("value") if isinstance(item, dict) and "value" in item else item


def _iso_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, (int, float)):
        return _utc(value)
    text = str(value).replace("Z", "+00:00")
    return _utc(datetime.fromisoformat(text))


class PostgresStorage:
    """Small DB-API adapter using psycopg 3 or an injected connection."""

    def __init__(self, connection: Any):
        self.connection = connection

    @classmethod
    def from_env(cls) -> "PostgresStorage":
        """Connect using SKY_SCANNER_DATABASE_URL; import psycopg only on use."""
        dsn = os.environ.get("SKY_SCANNER_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not dsn:
            raise RuntimeError("DATABASE_URL or SKY_SCANNER_DATABASE_URL is not configured")
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("psycopg 3 is required for PostgreSQL storage") from exc
        return cls(psycopg.connect(dsn))

    def init_schema(self, schema_path: str | Path | None = None) -> None:
        path = Path(schema_path) if schema_path else Path(__file__).resolve().parent.parent / "sql/schema.sql"
        self._execute_script(path.read_text(encoding="utf-8"))

    def register_receiver(
        self, receiver_id: str, description: str | None = None,
        latitude: float | None = None, longitude: float | None = None,
    ) -> None:
        if (latitude is None) != (longitude is None):
            raise ValueError("latitude and longitude must be supplied together")
        if latitude is None:
            sql = """INSERT INTO receivers(receiver_id, description) VALUES (%s, %s)
                ON CONFLICT(receiver_id) DO UPDATE SET description=EXCLUDED.description"""
            params = (receiver_id, description)
        else:
            sql = """INSERT INTO receivers(receiver_id, description, location)
                VALUES (%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography)
                ON CONFLICT(receiver_id) DO UPDATE SET description=EXCLUDED.description,
                location=EXCLUDED.location"""
            params = (receiver_id, description, longitude, latitude)
        self._execute(sql, params)

    def save_capture_interval(self, interval: dict[str, Any]) -> None:
        interval_id = interval.get("id") or uuid.uuid4()
        sql = """INSERT INTO capture_intervals
            (id, receiver_id, frequency_hz, started_at, ended_at, decoder_version, sample_config)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (id) DO UPDATE SET ended_at=EXCLUDED.ended_at,
            decoder_version=EXCLUDED.decoder_version, sample_config=EXCLUDED.sample_config"""
        params = (
            interval_id, interval["receiver_id"], int(interval["frequency_hz"]),
            _utc(interval["started_at"]),
            _utc(interval["ended_at"]) if interval.get("ended_at") is not None else None,
            interval.get("decoder_version"), json.dumps(interval.get("sample_config") or {}),
        )
        self._execute(sql, params)

    def save_snapshots(self, rows: Iterable[dict[str, Any]]) -> int:
        """Insert snapshots transactionally; duplicate composite keys are ignored."""
        params = [self._snapshot_params(row) for row in rows]
        if not params:
            return 0
        cursor = self.connection.cursor()
        try:
            cursor.executemany(_SNAPSHOT_INSERT, params)
            inserted = max(0, int(cursor.rowcount))
            self.connection.commit()
            return inserted
        except Exception:
            self.connection.rollback()
            raise
        finally:
            cursor.close()

    def save_metadata_import(
        self, records: Iterable[dict[str, Any]], *, source_url: str | None = None,
        retrieved_at: Any | None = None, dataset_hash: str | None = None,
        batch_size: int = 5000, source_name: str = "faa_registry", partial: bool = False,
    ) -> str:
        """Save import provenance and only the metadata versions that changed.

        Records are staged in a transaction-local temporary table. A new
        ``present`` version is written only when an aircraft is new or its
        registry fields differ from its latest version from the same ``source_name``; an
        ``absent`` version is written when a previously present aircraft is no
        longer listed. Unchanged daily refreshes therefore add no metadata rows.
        ``partial`` imports cover only some aircraft (for example, only those
        observed), so no ``absent`` versions are written for the rest.
        Uses the parser's provenance when import-level values are omitted.
        Returns the enrichment_imports UUID as text.
        """
        records = list(records)
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if records:
            source_url = source_url or records[0]["source_url"]
            retrieved_at = retrieved_at or records[0]["retrieved_at"]
            dataset_hash = dataset_hash or records[0]["dataset_hash"]
        if not source_url or retrieved_at is None or not dataset_hash:
            raise ValueError("source_url, retrieved_at, and dataset_hash are required")
        retrieved = _iso_datetime(retrieved_at)
        import_id = uuid.uuid4()
        cursor = self.connection.cursor()
        try:
            cursor.execute(
                """INSERT INTO enrichment_imports
                   (id, source_url, retrieved_at, dataset_hash, record_count, source_name)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (source_url, dataset_hash) DO UPDATE
                   SET record_count=EXCLUDED.record_count
                   RETURNING id""",
                (import_id, source_url, retrieved, dataset_hash, len(records), source_name),
            )
            row = cursor.fetchone()
            if row:
                import_id = row[0]
            cursor.execute(_METADATA_STAGE_CREATE)
            stage_sql = f"INSERT INTO sky_metadata_stage ({_METADATA_COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            for offset in range(0, len(records), batch_size):
                chunk = records[offset:offset + batch_size]
                values = [(
                    record.get("address_type", "icao"),
                    record.get("icao_address") or record.get("address"),
                    record.get("registration"), record.get("manufacturer"),
                    record.get("model"), record.get("aircraft_type"),
                    record.get("registered_owner"), record.get("operator"),
                    record.get("icao_type_code"), record.get("operator_icao"),
                    record.get("country"), record.get("category"), record.get("tags"),
                ) for record in chunk]
                if values:
                    cursor.executemany(stage_sql, values)
            cursor.execute("CREATE INDEX ON sky_metadata_stage (address_type, address)")
            params = {"source_name": source_name, "source_url": source_url, "retrieved_at": retrieved,
                      "dataset_hash": dataset_hash, "import_id": import_id}
            cursor.execute(_METADATA_INSERT_CHANGED, params)
            changed = max(0, int(cursor.rowcount or 0))
            if not partial:
                cursor.execute(_METADATA_INSERT_ABSENT, params)
                changed += max(0, int(cursor.rowcount or 0))
            cursor.execute("UPDATE enrichment_imports SET changed_count = %s WHERE id = %s",
                           (changed, import_id))
            self.connection.commit()
            return str(import_id)
        except Exception:
            self.connection.rollback()
            raise
        finally:
            cursor.close()

    def replace_military_operator_codes(
        self, codes: Iterable[dict[str, str]], *, source_url: str, retrieved_at: Any,
        dataset_hash: str,
    ) -> str:
        """Atomically replace the military operator code set, recording provenance."""
        codes = list(codes)
        if not codes:
            raise ValueError("refusing to replace military operator codes with an empty set")
        import_id = uuid.uuid4()
        cursor = self.connection.cursor()
        try:
            cursor.execute(
                """INSERT INTO enrichment_imports
                   (id, source_url, retrieved_at, dataset_hash, record_count, source_name)
                   VALUES (%s, %s, %s, %s, %s, 'military_operator_codes')
                   ON CONFLICT (source_url, dataset_hash) DO UPDATE
                   SET record_count=EXCLUDED.record_count
                   RETURNING id""",
                (import_id, source_url, _iso_datetime(retrieved_at), dataset_hash, len(codes)),
            )
            row = cursor.fetchone()
            if row:
                import_id = row[0]
            cursor.execute("DELETE FROM military_operator_codes")
            cursor.executemany(
                "INSERT INTO military_operator_codes (code, name, import_id) VALUES (%s, %s, %s)",
                [(c["code"], c["name"], import_id) for c in codes])
            self.connection.commit()
            return str(import_id)
        except Exception:
            self.connection.rollback()
            raise
        finally:
            cursor.close()

    def save_callword_lookup(self, word: str, status: str, source_url: str,
                             data: dict[str, Any] | None, fetched_at: Any | None = None) -> None:
        self._execute(
            """INSERT INTO callword_lookups (word, status, source_url, fetched_at, data)
               VALUES (%s, %s, %s, %s, %s::jsonb)
               ON CONFLICT (word) DO UPDATE SET status=EXCLUDED.status,
               source_url=EXCLUDED.source_url, fetched_at=EXCLUDED.fetched_at, data=EXCLUDED.data""",
            (word, status, source_url, _iso_datetime(fetched_at or datetime.now(timezone.utc)),
             json.dumps(data) if data is not None else None),
        )

    def save_type_image(self, designator: str, model_name: str | None, method: str,
                        result: dict[str, Any] | None, fetched_at: Any | None = None) -> None:
        result = result or {}
        self._execute(
            """INSERT INTO aircraft_type_images (designator, model_name, status, method, title,
                   description, article_url, image_url, file_url, fetched_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (designator) DO UPDATE SET model_name=EXCLUDED.model_name,
               status=EXCLUDED.status, method=EXCLUDED.method, title=EXCLUDED.title,
               description=EXCLUDED.description, article_url=EXCLUDED.article_url,
               image_url=EXCLUDED.image_url, file_url=EXCLUDED.file_url,
               fetched_at=EXCLUDED.fetched_at""",
            (designator, model_name, "found" if result else "not_found", method,
             result.get("title"), result.get("description"), result.get("article_url"),
             result.get("image_url"), result.get("file_url"),
             _iso_datetime(fetched_at or datetime.now(timezone.utc))),
        )

    @staticmethod
    def _snapshot_params(row: dict[str, Any]) -> tuple[Any, ...]:
        fields = row.get("fields") or {}
        typed = [_field_value(fields, name) for name in _SNAPSHOT_FIELDS]
        return (
            _utc(row["bucket_start"]), row["receiver_id"], row["address_type"],
            row["address"], int(row["frequency_hz"]),
            _utc(row["first_received_at"]), _utc(row["last_received_at"]),
            int(row["message_count"]), *typed, json.dumps(fields, separators=(",", ":")),
        )

    def _execute(self, sql: str, params: tuple[Any, ...]) -> None:
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql, params)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        finally:
            cursor.close()

    def _execute_script(self, sql: str) -> None:
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        finally:
            cursor.close()
