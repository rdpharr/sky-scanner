"""Enrichment's writes to BigQuery ``raw``.

Identity metadata is versioned: a source's import adds a ``present`` version
only for aircraft that are new or whose values changed since that source's
latest version, and (for full imports) an ``absent`` version for aircraft the
source stopped listing. Unchanged daily refreshes add no rows. The records
are loaded into a staging table with a free load job; ``_METADATA_SCRIPT``
then compares and inserts in one transaction, together with the import's
``enrichment_imports`` row, so a retried import never adds a version twice.

Lookups (callwords, type photos) and enrichment runs are appended by
streaming; staging keeps each key's latest lookup. Records follow the
parsers' shape (``enrichment.parse_faa_registry`` and friends).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from .bq import REPLICATED_AT, SPECS, BigQuery, TableSpec, _columns

_VALUE_COLUMNS = (
    "registration",
    "manufacturer",
    "model",
    "aircraft_type",
    "registered_owner",
    "operator",
    "icao_type_code",
    "operator_icao",
    "country",
    "category",
    "tags",
)
# Filled by importers after versions already existed; null on older versions.
_ADDED_COLUMNS = ("registrant_type", "year_mfr")

_STAGE_COLUMNS = _columns(
    "address_type STRING!",
    "address STRING!",
    *(f"{name} STRING" for name in (*_VALUE_COLUMNS, "registrant_type")),
    "year_mfr INT64",
    f"{REPLICATED_AT} TIMESTAMP!",
)

_ALL = ("address_type", "address", *_VALUE_COLUMNS, *_ADDED_COLUMNS)

# {stage} and {dataset} are replaced before running. A version is new when the
# aircraft has no version from this source, its latest is "absent", or a value
# differs; a field added later that is null on the latest version is unknown,
# not a change.
_METADATA_SCRIPT = f"""
DECLARE next_id INT64 DEFAULT (SELECT COALESCE(MAX(id), 0) FROM {{dataset}}.aircraft_metadata);
DECLARE import_id STRING DEFAULT COALESCE(
    (SELECT ANY_VALUE(id) FROM {{dataset}}.enrichment_imports
     WHERE source_url = @source_url AND dataset_hash = @dataset_hash),
    GENERATE_UUID());
DECLARE changed INT64;

CREATE TEMP TABLE latest AS
SELECT * FROM {{dataset}}.aircraft_metadata
WHERE source_name = @source_name
QUALIFY ROW_NUMBER() OVER (PARTITION BY address_type, address ORDER BY retrieved_at DESC, id DESC) = 1;

CREATE TEMP TABLE versions AS
SELECT {", ".join(f"s.{c}" for c in _ALL)}, 'present' AS registry_status
FROM {{stage}} AS s
LEFT JOIN latest AS l ON l.address_type = s.address_type AND l.address = s.address
WHERE l.id IS NULL
   OR l.registry_status != 'present'
   OR {" OR ".join(f"l.{c} IS DISTINCT FROM s.{c}" for c in _VALUE_COLUMNS)}
   OR {" OR ".join(f"(l.{c} IS NOT NULL AND l.{c} IS DISTINCT FROM s.{c})" for c in _ADDED_COLUMNS)}
UNION ALL
SELECT l.address_type, l.address, {", ".join(f"NULL AS {c}" for c in _VALUE_COLUMNS)},
       CAST(NULL AS STRING) AS registrant_type, CAST(NULL AS INT64) AS year_mfr, 'absent' AS registry_status
FROM latest AS l
WHERE NOT @partial
  AND l.registry_status = 'present'
  AND NOT EXISTS (SELECT 1 FROM {{stage}} AS s WHERE s.address_type = l.address_type AND s.address = l.address);

SET changed = (SELECT COUNT(*) FROM versions);

BEGIN TRANSACTION;

INSERT INTO {{dataset}}.aircraft_metadata
    (id, {", ".join(_ALL)}, registry_status, source_name, source_url, retrieved_at, dataset_hash,
     import_id, {REPLICATED_AT})
SELECT next_id + ROW_NUMBER() OVER (ORDER BY address_type, address), {", ".join(_ALL)}, registry_status,
       @source_name, @source_url, @retrieved_at, @dataset_hash, import_id, CURRENT_TIMESTAMP()
FROM versions;

MERGE {{dataset}}.enrichment_imports AS i
USING (SELECT import_id AS id) AS n
ON i.id = n.id
WHEN MATCHED THEN UPDATE SET
    record_count = @record_count,
    changed_count = COALESCE(i.changed_count, 0) + changed,
    {REPLICATED_AT} = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN INSERT
    (id, source_url, retrieved_at, dataset_hash, record_count, imported_at, changed_count, source_name,
     {REPLICATED_AT})
    VALUES (import_id, @source_url, @retrieved_at, @dataset_hash, @record_count, CURRENT_TIMESTAMP(), changed,
            @source_name, CURRENT_TIMESTAMP());

COMMIT TRANSACTION;

DROP TABLE {{stage}};

SELECT import_id, changed;
"""

_IMPORT_SCRIPT = f"""
MERGE {{dataset}}.enrichment_imports AS i
USING (SELECT @source_url AS source_url, @dataset_hash AS dataset_hash) AS n
ON i.source_url = n.source_url AND i.dataset_hash = n.dataset_hash
WHEN MATCHED THEN UPDATE SET record_count = @record_count, {REPLICATED_AT} = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN INSERT
    (id, source_url, retrieved_at, dataset_hash, record_count, imported_at, changed_count, source_name,
     {REPLICATED_AT})
    VALUES (@import_id, @source_url, @retrieved_at, @dataset_hash, @record_count, CURRENT_TIMESTAMP(),
            @record_count, @source_name, CURRENT_TIMESTAMP())
"""

# Tables enrichment writes; doc8643_types is created by its CSV load.
_TABLES = (
    "aircraft_metadata",
    "enrichment_imports",
    "military_operator_codes",
    "callword_lookups",
    "aircraft_type_images",
    "enrichment_runs",
)


def _iso_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)):
        result = datetime.fromtimestamp(float(value), UTC)
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _integer(value: Any) -> int | None:
    try:
        return None if value in (None, "") else int(value)
    except (TypeError, ValueError):
        return None


def stage_rows(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """One staging row per address type and address (the first record wins)."""
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        address_type = record.get("address_type", "icao")
        address = record.get("icao_address") or record.get("address")
        if (address_type, address) in rows:
            continue
        row = {"address_type": address_type, "address": address}
        for name in (*_VALUE_COLUMNS, "registrant_type"):
            row[name] = _text(record.get(name))
        row["year_mfr"] = _integer(record.get("year_mfr"))
        rows[(address_type, address)] = row
    return list(rows.values())


def _stage_spec(source_name: str) -> TableSpec:
    if not re.fullmatch(r"[a-z0-9_]+", source_name):
        raise ValueError(f"invalid source name {source_name!r}")
    return TableSpec(f"_stage_metadata_{source_name}", _STAGE_COLUMNS)


class BigQueryStorage:
    """The enrichment worker's storage, backed by BigQuery ``raw``."""

    def __init__(self, bigquery: BigQuery):
        self.bigquery = bigquery
        self._ready = False

    def _ensure(self) -> None:
        if not self._ready:
            for name in _TABLES:
                self.bigquery.ensure_table(SPECS[name])
            self._ready = True

    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self._ensure()
        return self.bigquery.query(sql, params)

    def import_exists(self, source_url: str, dataset_hash: str | None = None) -> bool:
        """Has this dataset (or, without a hash, anything from this URL) been imported?"""
        condition = " AND dataset_hash = @dataset_hash" if dataset_hash else ""
        params = {"source_url": source_url, **({"dataset_hash": dataset_hash} if dataset_hash else {})}
        rows = self.query(
            f"SELECT COUNT(*) AS n FROM {{dataset}}.enrichment_imports WHERE source_url = @source_url{condition}",
            params,
        )
        return bool(rows and rows[0]["n"])

    def save_metadata_import(
        self,
        records: Iterable[dict[str, Any]],
        *,
        source_url: str | None = None,
        retrieved_at: Any | None = None,
        dataset_hash: str | None = None,
        source_name: str = "faa_registry",
        partial: bool = False,
    ) -> str:
        """Save only the metadata versions that changed; returns the import ID.

        ``partial`` imports cover only some aircraft (for example, only those
        heard), so no ``absent`` versions are written for the rest. Uses the
        parser's provenance when import-level values are omitted.
        """
        records = list(records)
        if records:
            source_url = source_url or records[0].get("source_url")
            retrieved_at = retrieved_at or records[0].get("retrieved_at")
            dataset_hash = dataset_hash or records[0].get("dataset_hash")
        if not source_url or retrieved_at is None or not dataset_hash:
            raise ValueError("source_url, retrieved_at, and dataset_hash are required")
        if not records and not partial:
            raise ValueError("refusing a full import with no records: every aircraft would become absent")
        self._ensure()
        stage = _stage_spec(source_name)
        self.bigquery.load(stage, stage_rows(records), truncate=True)
        script = _METADATA_SCRIPT.replace("{stage}", "{dataset}." + stage.name)
        (result,) = self.bigquery.query(
            script,
            {
                "source_name": source_name,
                "source_url": source_url,
                "retrieved_at": _iso_datetime(retrieved_at),
                "dataset_hash": dataset_hash,
                "record_count": len(records),
                "partial": partial,
            },
        )
        return str(result["import_id"])

    def replace_military_operator_codes(
        self, codes: Iterable[dict[str, str]], *, source_url: str, retrieved_at: Any, dataset_hash: str
    ) -> str:
        """Replace the military operator code set, then record the import."""
        codes = list(codes)
        if not codes:
            raise ValueError("refusing to replace military operator codes with an empty set")
        self._ensure()
        import_id = str(uuid.uuid4())
        rows = [{"code": c["code"], "name": c["name"], "import_id": import_id} for c in codes]
        self.bigquery.load(SPECS["military_operator_codes"], rows, truncate=True)
        # Recorded last: an import row means the codes are in place.
        self.bigquery.query(
            _IMPORT_SCRIPT,
            {
                "import_id": import_id,
                "source_name": "military_operator_codes",
                "source_url": source_url,
                "retrieved_at": _iso_datetime(retrieved_at),
                "dataset_hash": dataset_hash,
                "record_count": len(codes),
            },
        )
        return import_id

    def save_callword_lookup(
        self, word: str, status: str, source_url: str, data: dict[str, Any] | None, fetched_at: Any | None = None
    ) -> None:
        self._ensure()
        row = {
            "word": word,
            "status": status,
            "source_url": source_url,
            "fetched_at": _iso_datetime(datetime.now(UTC) if fetched_at is None else fetched_at),
            "data": data,
        }
        self.bigquery.stream(SPECS["callword_lookups"], [row])

    def save_type_image(
        self,
        designator: str,
        model_name: str | None,
        method: str,
        result: dict[str, Any] | None,
        fetched_at: Any | None = None,
    ) -> None:
        self._ensure()
        result = result or {}
        row = {
            "designator": designator,
            "model_name": model_name,
            "status": "found" if result else "not_found",
            "method": method,
            **{k: result.get(k) for k in ("title", "description", "article_url", "image_url", "file_url")},
            "fetched_at": _iso_datetime(datetime.now(UTC) if fetched_at is None else fetched_at),
        }
        self.bigquery.stream(SPECS["aircraft_type_images"], [row])

    def load_reference_csv(self, table: str, data: bytes) -> None:
        self.bigquery.load_csv(table, data)

    def record_run(
        self,
        worker: str,
        started_at: datetime,
        finished_at: datetime,
        status: str,
        summary: dict[str, Any] | None,
        error: str | None,
    ) -> None:
        self._ensure()
        row = {
            "worker": worker,
            "started_at": started_at,
            "finished_at": finished_at,
            "status": status,
            "summary": summary,
            "error": error,
        }
        self.bigquery.stream(SPECS["enrichment_runs"], [row])
