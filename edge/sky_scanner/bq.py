"""BigQuery ``raw`` tables the edge writes, and a small client for them.

The schemas of the replicated tables were copied from the tables the
replicator created (2026-10-19), so existing columns keep their names, types
and modes. ``doc8643_types`` is not listed: it is loaded from a CSV whose
header sets its columns.

Rows are streamed with insert IDs built from each table's key (plus
``written_at`` when the table has one), so a retried batch is deduplicated by
BigQuery on a best-effort basis; dbt staging removes what remains. Every
streamed or loaded row carries ``_replicated_at``, the time the edge sent it.

``google.cloud.bigquery`` is imported only when a client is used, so the
pure parts of this module work without it.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

log = logging.getLogger(__name__)

REPLICATED_AT = "_replicated_at"
STREAM_BATCH_ROWS = 500
DEFAULT_MAX_BYTES_BILLED = 1_000_000_000
# Retries give up after this long, so callers see an outage instead of a ten-minute wait.
RETRY_SECONDS = 60
REQUEST_SECONDS = 30


@dataclass(frozen=True)
class Column:
    name: str
    bq_type: str
    nullable: bool


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: tuple[Column, ...]
    key: tuple[str, ...] = ()
    partition: str | None = None
    cluster: tuple[str, ...] = ()

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)


def _columns(*fields: str) -> tuple[Column, ...]:
    """Columns from "name TYPE" ("name TYPE!" when required)."""
    result = []
    for spec in fields:
        name, bq_type = spec.split()
        result.append(Column(name, bq_type.rstrip("!"), not bq_type.endswith("!")))
    return tuple(result)


TABLES = (
    TableSpec(
        "aircraft_snapshots",
        _columns(
            "bucket_start TIMESTAMP!",
            "receiver_id STRING!",
            "address_type STRING!",
            "address STRING!",
            "frequency_hz INT64!",
            "first_received_at TIMESTAMP!",
            "last_received_at TIMESTAMP!",
            "message_count INT64!",
            "callsign STRING",
            "latitude FLOAT64",
            "longitude FLOAT64",
            "altitude_baro_ft FLOAT64",
            "altitude_geom_ft FLOAT64",
            "ground_speed_kt FLOAT64",
            "track_deg FLOAT64",
            "vertical_rate_baro_fpm FLOAT64",
            "vertical_rate_geom_fpm FLOAT64",
            "squawk STRING",
            "on_ground BOOL",
            "fields JSON!",
            "written_at TIMESTAMP!",
            "_replicated_at TIMESTAMP!",
        ),
        key=("bucket_start", "receiver_id", "address_type", "address", "frequency_hz"),
        partition="bucket_start",
        cluster=("address",),
    ),
    TableSpec(
        "capture_intervals",
        _columns(
            "id STRING!",
            "receiver_id STRING!",
            "frequency_hz INT64!",
            "started_at TIMESTAMP!",
            "ended_at TIMESTAMP",
            "decoder_version STRING",
            "sample_config JSON!",
            "written_at TIMESTAMP!",
            "_replicated_at TIMESTAMP!",
        ),
        key=("id",),
        partition="started_at",
        cluster=("frequency_hz",),
    ),
    TableSpec(
        "aircraft_metadata",
        _columns(
            "id INT64!",
            "address_type STRING!",
            "address STRING!",
            "registration STRING",
            "manufacturer STRING",
            "model STRING",
            "aircraft_type STRING",
            "registered_owner STRING",
            "operator STRING",
            "source_url STRING!",
            "retrieved_at TIMESTAMP!",
            "dataset_hash STRING!",
            "import_id STRING",
            "registry_status STRING!",
            "source_name STRING!",
            "icao_type_code STRING",
            "operator_icao STRING",
            "country STRING",
            "category STRING",
            "tags STRING",
            "registrant_type STRING",
            "year_mfr INT64",
            "_replicated_at TIMESTAMP!",
        ),
        key=("id",),
        cluster=("source_name", "address"),
    ),
    TableSpec(
        "enrichment_imports",
        _columns(
            "id STRING!",
            "source_url STRING!",
            "retrieved_at TIMESTAMP!",
            "dataset_hash STRING!",
            "record_count INT64!",
            "imported_at TIMESTAMP!",
            "changed_count INT64",
            "source_name STRING!",
            "_replicated_at TIMESTAMP!",
        ),
        key=("id",),
    ),
    TableSpec(
        "military_operator_codes",
        _columns(
            "code STRING!",
            "name STRING!",
            "import_id STRING",
            "_replicated_at TIMESTAMP!",
        ),
        key=("code",),
    ),
    TableSpec(
        "callword_lookups",
        _columns(
            "word STRING!",
            "status STRING!",
            "source_url STRING!",
            "fetched_at TIMESTAMP!",
            "data JSON",
            "_replicated_at TIMESTAMP!",
        ),
        key=("word", "fetched_at"),
    ),
    TableSpec(
        "aircraft_type_images",
        _columns(
            "designator STRING!",
            "model_name STRING",
            "status STRING!",
            "method STRING",
            "title STRING",
            "description STRING",
            "article_url STRING",
            "image_url STRING",
            "file_url STRING",
            "fetched_at TIMESTAMP!",
            "_replicated_at TIMESTAMP!",
        ),
        key=("designator", "fetched_at"),
    ),
    TableSpec(
        "receivers",
        _columns(
            "receiver_id STRING!",
            "description STRING",
            "created_at TIMESTAMP!",
            "_replicated_at TIMESTAMP!",
        ),
        key=("receiver_id",),
    ),
    # One row per enrichment worker cycle; its freshness shows enrichment is alive.
    TableSpec(
        "enrichment_runs",
        _columns(
            "worker STRING!",
            "started_at TIMESTAMP!",
            "finished_at TIMESTAMP!",
            "status STRING!",
            "summary JSON",
            "error STRING",
            "_replicated_at TIMESTAMP!",
        ),
        key=("worker", "started_at"),
        partition="started_at",
    ),
)

SPECS = {spec.name: spec for spec in TABLES}

# The API reports legacy names for some standard SQL types.
_TYPE_ALIASES = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL", "RECORD": "STRUCT"}


class RowsRejected(RuntimeError):
    """BigQuery refused the rows themselves; sending them again cannot succeed."""


def to_json_value(value: Any, *, json_as_text: bool = False) -> Any:
    """Convert a Python value for a BigQuery JSON request."""
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat() if value.tzinfo else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if json_as_text and isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    return value


def json_row(spec: TableSpec, row: dict[str, Any]) -> dict[str, Any]:
    """A row as streaming expects it: JSON columns as text, times as ISO strings."""
    json_columns = {c.name for c in spec.columns if c.bq_type == "JSON"}
    return {k: to_json_value(v, json_as_text=k in json_columns) for k, v in row.items()}


def row_id(spec: TableSpec, row: dict[str, Any]) -> str:
    """Insert ID: the key, plus ``written_at`` when the table has one."""
    names = (*spec.key, "written_at") if "written_at" in spec.column_names else spec.key
    if not names:
        raise ValueError(f"table {spec.name} has no key to build insert IDs from")
    parts = [str(to_json_value(row[name])) for name in names]
    return hashlib.sha1("\x1f".join(parts).encode()).hexdigest()


def schema_changes(spec: TableSpec, existing: Iterable[tuple[str, str]]) -> tuple[list[Column], list[str]]:
    """Columns missing from an existing table, and columns whose type differs."""
    types = {name: _TYPE_ALIASES.get(bq_type, bq_type) for name, bq_type in existing}
    added = [c for c in spec.columns if c.name not in types]
    mismatched = [
        f"{c.name}: {types[c.name]} in BigQuery, {c.bq_type} expected"
        for c in spec.columns
        if c.name in types and types[c.name] != c.bq_type
    ]
    return added, mismatched


def _parameter_type(value: Any) -> str:
    if isinstance(value, bool):
        return "BOOL"
    if isinstance(value, int):
        return "INT64"
    if isinstance(value, float):
        return "FLOAT64"
    if isinstance(value, datetime):
        return "TIMESTAMP"
    return "STRING"


class BigQuery:
    """The edge's access to one dataset: create tables, stream, load, query."""

    def __init__(self, client: Any, dataset: str, *, max_bytes_billed: int = DEFAULT_MAX_BYTES_BILLED):
        self.client, self.dataset, self.max_bytes_billed = client, dataset, max_bytes_billed

    @classmethod
    def from_env(cls, environ: dict[str, str]) -> BigQuery:
        from google.cloud import bigquery

        client = bigquery.Client(project=environ["BQ_PROJECT"])
        return cls(client, environ.get("BQ_DATASET", "raw"))

    def ref(self, table: str) -> str:
        return f"{self.client.project}.{self.dataset}.{table}"

    @staticmethod
    def _retry():
        from google.cloud import bigquery

        return bigquery.DEFAULT_RETRY.with_timeout(RETRY_SECONDS)

    @staticmethod
    def _schema(columns: Iterable[Column]):
        from google.cloud import bigquery

        return [bigquery.SchemaField(c.name, c.bq_type, mode="NULLABLE" if c.nullable else "REQUIRED") for c in columns]

    def ensure_table(self, spec: TableSpec) -> None:
        """Create the table if missing; add new columns (as NULLABLE) if not.

        A column whose type differs is an error: writing would fail or, worse,
        coerce values silently.
        """
        from google.api_core.exceptions import NotFound
        from google.cloud import bigquery

        try:
            table = self.client.get_table(self.ref(spec.name), retry=self._retry(), timeout=REQUEST_SECONDS)
        except NotFound:
            table = bigquery.Table(self.ref(spec.name), schema=self._schema(spec.columns))
            if spec.partition:
                table.time_partitioning = bigquery.TimePartitioning(field=spec.partition)
            if spec.cluster:
                table.clustering_fields = list(spec.cluster)
            self.client.create_table(table, retry=self._retry(), timeout=REQUEST_SECONDS)
            log.info("created %s", table.table_id)
            return
        added, mismatched = schema_changes(spec, ((f.name, f.field_type) for f in table.schema))
        if mismatched:
            raise RuntimeError(f"{spec.name} schema differs: {'; '.join(mismatched)}")
        if added:
            # New columns are NULLABLE so existing rows stay valid.
            table.schema = list(table.schema) + self._schema(Column(c.name, c.bq_type, True) for c in added)
            self.client.update_table(table, ["schema"], retry=self._retry(), timeout=REQUEST_SECONDS)
            log.info("added columns to %s: %s", spec.name, [c.name for c in added])

    def stream(self, spec: TableSpec, rows: list[dict[str, Any]], *, sent_at: datetime | None = None) -> int:
        """Stream rows with insert IDs; raises RowsRejected if BigQuery refuses them."""
        stamp = sent_at or datetime.now(UTC)
        prepared = [{**row, REPLICATED_AT: stamp} for row in rows]
        for start in range(0, len(prepared), STREAM_BATCH_ROWS):
            batch = prepared[start : start + STREAM_BATCH_ROWS]
            errors = self.client.insert_rows_json(
                self.ref(spec.name),
                [json_row(spec, r) for r in batch],
                row_ids=[row_id(spec, r) for r in batch],
                retry=self._retry(),
                timeout=REQUEST_SECONDS,
            )
            if errors:
                raise RowsRejected(f"BigQuery rejected rows for {spec.name}: {errors[:3]}")
        return len(prepared)

    def load(self, spec: TableSpec, rows: Iterable[dict[str, Any]], *, truncate: bool = False) -> int:
        """Load rows with a (free) load job, appending or replacing the table."""
        from google.cloud import bigquery

        stamp = datetime.now(UTC)
        count = 0
        with tempfile.TemporaryFile() as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb") as out:
                for row in rows:
                    line = {k: to_json_value(v) for k, v in {**row, REPLICATED_AT: stamp}.items()}
                    out.write(json.dumps(line, separators=(",", ":")).encode() + b"\n")
                    count += 1
            raw.seek(0)
            config = bigquery.LoadJobConfig(
                source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
                schema=self._schema(spec.columns),
                write_disposition="WRITE_TRUNCATE" if truncate else "WRITE_APPEND",
            )
            if spec.partition:
                config.time_partitioning = bigquery.TimePartitioning(field=spec.partition)
            if spec.cluster:
                config.clustering_fields = list(spec.cluster)
            if count or truncate:
                self.client.load_table_from_file(raw, self.ref(spec.name), job_config=config).result()
        return count

    def load_csv(self, table: str, data: bytes) -> None:
        """Replace ``table`` with a CSV, every column as text named by the header.

        ``_row_number`` keeps file order, which consumers may rely on (first
        entry wins); a load job does not preserve it.
        """
        import csv
        import io

        from google.cloud import bigquery

        reader = csv.reader(io.StringIO(data.decode("utf-8-sig")))
        header = [name.strip() for name in next(reader)]
        out = io.StringIO()
        writer = csv.writer(out)
        for number, row in enumerate(reader, 1):
            writer.writerow([number, *row])
        config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.CSV,
            schema=[bigquery.SchemaField("_row_number", "INT64")]
            + [bigquery.SchemaField(name, "STRING") for name in header],
            write_disposition="WRITE_TRUNCATE",
        )
        self.client.load_table_from_file(
            io.BytesIO(out.getvalue().encode()), self.ref(table), job_config=config
        ).result()

    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Run SQL (``{dataset}`` is replaced by the dataset's full name) under the byte cap."""
        from google.cloud import bigquery

        parameters = []
        for name, value in (params or {}).items():
            if isinstance(value, (list, tuple, set, frozenset)):
                values = list(value)
                kind = _parameter_type(values[0]) if values else "STRING"
                parameters.append(bigquery.ArrayQueryParameter(name, kind, values))
            else:
                parameters.append(bigquery.ScalarQueryParameter(name, _parameter_type(value), value))
        config = bigquery.QueryJobConfig(query_parameters=parameters, maximum_bytes_billed=self.max_bytes_billed)
        text = sql.replace("{dataset}", f"`{self.client.project}.{self.dataset}`")
        job = self.client.query(text, job_config=config, retry=self._retry(), timeout=REQUEST_SECONDS)
        return [dict(row.items()) for row in job.result(retry=self._retry())]
