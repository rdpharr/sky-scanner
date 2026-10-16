"""Replicate edge Postgres tables into the BigQuery ``raw`` dataset.

Live tables (snapshots, capture intervals) are polled every
``REPLICATOR_INTERVAL_SECONDS`` and streamed in order of ``written_at``. Each
poll re-reads a short overlap so rows from transactions that committed late
are not missed; rows already sent are skipped. Duplicates remain possible
(for example after a restart) and are removed in dbt staging.

Reference tables are reloaded in full once a day with load jobs, which are
free. An empty live table is filled by a single load job first (the
backfill). Every replicated row carries ``_replicated_at``.

Columns are mapped from the Postgres catalog, so new edge columns appear in
BigQuery automatically. Generated columns are skipped, and the receiver's
``location`` never leaves home.

A few reference lists that the edge fetches at runtime rather than storing
(``REFERENCE_URLS``) are loaded from their URLs on the same daily schedule.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import signal
import tempfile
import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

log = logging.getLogger("sky_scanner.replicator")

REPLICATED_AT = "_replicated_at"


@dataclass(frozen=True)
class TableSpec:
    name: str
    mode: str  # "stream" (by written_at) or "reload" (daily full copy)
    key: tuple[str, ...] = ()
    partition: str | None = None
    cluster: tuple[str, ...] = ()
    exclude: frozenset[str] = frozenset()


TABLES = (
    TableSpec(
        "aircraft_snapshots",
        "stream",
        key=("bucket_start", "receiver_id", "address_type", "address", "frequency_hz"),
        partition="bucket_start",
        cluster=("address",),
    ),
    TableSpec("capture_intervals", "stream", key=("id",), partition="started_at", cluster=("frequency_hz",)),
    TableSpec("receivers", "reload", exclude=frozenset({"location"})),
    TableSpec("aircraft_metadata", "reload", cluster=("source_name", "address")),
    TableSpec("enrichment_imports", "reload"),
    TableSpec("military_operator_codes", "reload"),
    TableSpec("callword_lookups", "reload"),
    TableSpec("aircraft_type_images", "reload"),
)

# Published CSVs loaded as text columns named by their header row.
REFERENCE_URLS = {
    # ICAO Doc 8643 aircraft type designators, as used by the aircraft log page.
    "doc8643_types": "https://s3.opensky-network.org/data-samples/metadata/doc8643AircraftTypes.csv",
}
MAX_REFERENCE_BYTES = 20 * 1024 * 1024

_TYPES = {
    "text": "STRING",
    "character varying": "STRING",
    "character": "STRING",
    "uuid": "STRING",
    "smallint": "INT64",
    "integer": "INT64",
    "bigint": "INT64",
    "real": "FLOAT64",
    "double precision": "FLOAT64",
    "numeric": "NUMERIC",
    "boolean": "BOOL",
    "timestamp with time zone": "TIMESTAMP",
    "timestamp without time zone": "DATETIME",
    "date": "DATE",
    "json": "JSON",
    "jsonb": "JSON",
}


@dataclass(frozen=True)
class Column:
    name: str
    bq_type: str
    nullable: bool


def bq_type(pg_type: str) -> str:
    try:
        return _TYPES[pg_type]
    except KeyError:
        raise ValueError(f"no BigQuery type for Postgres type {pg_type!r}") from None


def to_json_value(value: Any, *, json_as_text: bool = False) -> Any:
    """Convert a Postgres value for a BigQuery JSON request."""
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat() if value.tzinfo else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if json_as_text and isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    return value


class Source(Protocol):
    def columns(self, table: str) -> list[tuple[str, str, bool, bool]]: ...
    def now(self) -> datetime: ...
    def rows(
        self, table: str, columns: list[str], after: datetime | None = None, until: datetime | None = None
    ) -> Iterator[dict[str, Any]]: ...


class Sink(Protocol):
    def ensure_table(self, spec: TableSpec, schema: list[Column]) -> None: ...
    def max_written_at(self, table: str) -> datetime | None: ...
    def load(self, spec: TableSpec, schema: list[Column], rows: Iterable[dict[str, Any]], truncate: bool) -> int: ...
    def stream(self, table: str, rows: list[dict[str, Any]], row_ids: list[str]) -> None: ...
    def load_csv(self, table: str, data: bytes) -> None: ...


@dataclass
class StreamState:
    watermark: datetime
    sent: dict[str, datetime] = field(default_factory=dict)


class Replicator:
    def __init__(
        self,
        source: Source,
        sink: Sink,
        tables: Iterable[TableSpec] = TABLES,
        overlap: timedelta = timedelta(minutes=2),
        batch_size: int = 500,
        clock=lambda: datetime.now(UTC),
        urls: dict[str, str] | None = None,
        fetch=None,
    ):
        self.source, self.sink = source, sink
        self.tables = tuple(tables)
        self.urls = dict(REFERENCE_URLS if urls is None else urls)
        self.fetch = fetch or _fetch
        self.overlap, self.batch_size, self.clock = overlap, batch_size, clock
        self.schemas: dict[str, list[Column]] = {}
        self.streams: dict[str, StreamState] = {}

    def schema(self, spec: TableSpec) -> list[Column]:
        """Columns to replicate: catalog order, minus generated and excluded ones."""
        columns = [
            Column(name, bq_type(pg_type), nullable)
            for name, pg_type, nullable, generated in self.source.columns(spec.name)
            if not generated and name not in spec.exclude
        ]
        if not columns:
            raise RuntimeError(f"table {spec.name} has no replicable columns")
        return columns + [Column(REPLICATED_AT, "TIMESTAMP", False)]

    def prepare(self) -> None:
        for spec in self.tables:
            schema = self.schema(spec)
            self.schemas[spec.name] = schema
            self.sink.ensure_table(spec, schema)

    def _rows(self, spec: TableSpec, **window) -> Iterator[dict[str, Any]]:
        names = [c.name for c in self.schemas[spec.name] if c.name != REPLICATED_AT]
        stamp = self.clock()
        for row in self.source.rows(spec.name, names, **window):
            row[REPLICATED_AT] = stamp
            yield row

    def reload(self, spec: TableSpec) -> int:
        count = self.sink.load(spec, self.schemas[spec.name], self._rows(spec), truncate=True)
        log.info("reloaded %s: %d rows", spec.name, count)
        return count

    def reload_url(self, table: str) -> None:
        data = self.fetch(self.urls[table])
        self.sink.load_csv(table, data)
        log.info("reloaded %s from %s: %d bytes", table, self.urls[table], len(data))

    def start_stream(self, spec: TableSpec) -> None:
        """Resume from what BigQuery already holds, or backfill an empty table."""
        watermark = self.sink.max_written_at(spec.name)
        if watermark is None:
            cutoff = self.source.now()
            state = StreamState(cutoff)

            def remember_overlap(rows):
                # The first poll re-reads this window; these rows are loaded.
                for row in rows:
                    if row["written_at"] > cutoff - self.overlap:
                        state.sent[self._row_id(spec, row)] = row["written_at"]
                    yield row

            count = self.sink.load(
                spec, self.schemas[spec.name], remember_overlap(self._rows(spec, until=cutoff)), truncate=False
            )
            log.info("backfilled %s: %d rows written up to %s", spec.name, count, cutoff)
            self.streams[spec.name] = state
            return
        self.streams[spec.name] = StreamState(watermark)

    def _row_id(self, spec: TableSpec, row: dict[str, Any]) -> str:
        parts = [str(to_json_value(row[k])) for k in (*spec.key, "written_at")]
        return hashlib.sha1("\x1f".join(parts).encode()).hexdigest()

    def stream_once(self, spec: TableSpec) -> int:
        state = self.streams[spec.name]
        rows, ids = [], []
        for row in self._rows(spec, after=state.watermark - self.overlap):
            row_id = self._row_id(spec, row)
            if row_id not in state.sent:
                rows.append(row)
                ids.append(row_id)
        json_columns = {c.name for c in self.schemas[spec.name] if c.bq_type == "JSON"}
        for i in range(0, len(rows), self.batch_size):
            batch = [
                {k: to_json_value(v, json_as_text=k in json_columns) for k, v in r.items()}
                for r in rows[i : i + self.batch_size]
            ]
            self.sink.stream(spec.name, batch, ids[i : i + self.batch_size])
        # Only after every batch succeeded: remember and advance.
        for row, row_id in zip(rows, ids):
            state.sent[row_id] = row["written_at"]
            state.watermark = max(state.watermark, row["written_at"])
        horizon = state.watermark - self.overlap
        state.sent = {k: t for k, t in state.sent.items() if t >= horizon}
        return len(rows)


class PostgresSource:
    def __init__(self, connection):
        self.connection = connection

    def columns(self, table: str) -> list[tuple[str, str, bool, bool]]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """SELECT column_name, data_type, is_nullable = 'YES', is_generated = 'ALWAYS'
                   FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = %s
                   ORDER BY ordinal_position""",
                (table,),
            )
            return [tuple(r) for r in cursor.fetchall()]

    def now(self) -> datetime:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT clock_timestamp()")
            return cursor.fetchone()[0]

    def rows(self, table, columns, after=None, until=None):
        from psycopg import sql

        query = sql.SQL("SELECT {} FROM {}").format(
            sql.SQL(", ").join(map(sql.Identifier, columns)), sql.Identifier(table)
        )
        conditions, params = [], []
        if after is not None:
            conditions.append(sql.SQL("written_at > %s"))
            params.append(after)
        if until is not None:
            conditions.append(sql.SQL("written_at <= %s"))
            params.append(until)
        if conditions:
            query += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conditions)
        if after is not None or until is not None:
            query += sql.SQL(" ORDER BY written_at")
        # A server-side cursor keeps large reloads out of memory.
        with self.connection.transaction(), self.connection.cursor(name=f"replicate_{table}") as cursor:
            cursor.itersize = 5000
            cursor.execute(query, params)
            for record in cursor:
                yield dict(zip(columns, record))


class BigQuerySink:
    def __init__(self, client, dataset: str):
        self.client, self.dataset = client, dataset

    def _ref(self, table: str) -> str:
        return f"{self.client.project}.{self.dataset}.{table}"

    def _schema(self, schema: list[Column]):
        from google.cloud import bigquery

        return [bigquery.SchemaField(c.name, c.bq_type, mode="NULLABLE" if c.nullable else "REQUIRED") for c in schema]

    def ensure_table(self, spec, schema):
        from google.api_core.exceptions import NotFound
        from google.cloud import bigquery

        fields = self._schema(schema)
        try:
            table = self.client.get_table(self._ref(spec.name))
        except NotFound:
            table = bigquery.Table(self._ref(spec.name), schema=fields)
            if spec.partition:
                table.time_partitioning = bigquery.TimePartitioning(field=spec.partition)
            if spec.cluster:
                table.clustering_fields = list(spec.cluster)
            self.client.create_table(table)
            log.info("created %s", table.table_id)
            return
        existing = {f.name for f in table.schema}
        added = [f for f in fields if f.name not in existing]
        if added:
            # New columns are added as NULLABLE so existing rows stay valid.
            table.schema = list(table.schema) + [f.__class__(f.name, f.field_type, mode="NULLABLE") for f in added]
            self.client.update_table(table, ["schema"])
            log.info("added columns to %s: %s", spec.name, [f.name for f in added])

    def max_written_at(self, table):
        result = self.client.query(f"SELECT MAX(written_at) AS m FROM `{self._ref(table)}`").result()
        return next(iter(result)).m

    def load(self, spec, schema, rows, truncate):
        from google.cloud import bigquery

        count = 0
        with tempfile.TemporaryFile() as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb") as out:
                for row in rows:
                    out.write(
                        json.dumps({k: to_json_value(v) for k, v in row.items()}, separators=(",", ":")).encode()
                        + b"\n"
                    )
                    count += 1
            raw.seek(0)
            config = bigquery.LoadJobConfig(
                source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
                schema=self._schema(schema),
                write_disposition="WRITE_TRUNCATE" if truncate else "WRITE_APPEND",
            )
            if spec.partition:
                config.time_partitioning = bigquery.TimePartitioning(field=spec.partition)
            if spec.cluster:
                config.clustering_fields = list(spec.cluster)
            if count or truncate:
                self.client.load_table_from_file(raw, self._ref(spec.name), job_config=config).result()
        return count

    def load_csv(self, table, data):
        import csv
        import io

        from google.cloud import bigquery

        # Column names come from the header; values stay text for dbt to type.
        # _row_number keeps file order, which consumers may rely on (first
        # entry wins); a load job does not preserve it.
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
            io.BytesIO(out.getvalue().encode()), self._ref(table), job_config=config
        ).result()

    def stream(self, table, rows, row_ids):
        errors = self.client.insert_rows_json(self._ref(table), rows, row_ids=row_ids)
        if errors:
            raise RuntimeError(f"BigQuery rejected rows for {table}: {errors[:3]}")


def _fetch(url: str, timeout: int = 60) -> bytes:
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": "SkyScanner/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_REFERENCE_BYTES + 1)
    if len(data) > MAX_REFERENCE_BYTES:
        raise ValueError(f"{url} is larger than {MAX_REFERENCE_BYTES} bytes")
    return data


def run(
    replicator: Replicator, stop: threading.Event, interval: float, reload_every: timedelta, max_failures: int = 5
) -> None:
    """Replicate until stopped.

    A failed table is retried on the next poll with its watermark (or reload
    schedule) unchanged. After ``max_failures`` polls in a row with a failure,
    the error is raised so the caller reconnects and starts over.
    """
    replicator.prepare()
    for spec in replicator.tables:
        if spec.mode == "stream":
            replicator.start_stream(spec)
    epoch = datetime.min.replace(tzinfo=UTC)
    next_reload = {s.name: epoch for s in replicator.tables if s.mode == "reload"}
    next_reload.update({name: epoch for name in replicator.urls})
    streamed, last_report, failures = 0, replicator.clock(), 0
    while not stop.is_set():
        now = replicator.clock()
        error = None
        for spec in replicator.tables:
            if spec.mode == "reload" and now < next_reload[spec.name]:
                continue
            try:
                if spec.mode == "reload":
                    replicator.reload(spec)
                    next_reload[spec.name] = now + reload_every
                else:
                    streamed += replicator.stream_once(spec)
            except Exception as exc:
                error = exc
                log.exception("replicating %s failed; retrying next poll", spec.name)
        for name in replicator.urls:
            if now < next_reload[name]:
                continue
            try:
                replicator.reload_url(name)
                next_reload[name] = now + reload_every
            except Exception as exc:
                error = exc
                log.exception("loading %s failed; retrying next poll", name)
        failures = failures + 1 if error else 0
        if failures >= max_failures:
            raise error
        if now - last_report >= timedelta(minutes=10):
            lag = {n: round((now - s.watermark).total_seconds()) for n, s in replicator.streams.items()}
            log.info("streamed %d rows in the last 10 min; seconds since newest row: %s", streamed, lag)
            streamed, last_report = 0, now
        stop.wait(interval)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import psycopg
    from google.cloud import bigquery

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    interval = float(os.environ.get("REPLICATOR_INTERVAL_SECONDS", "30"))
    reload_every = timedelta(hours=float(os.environ.get("REPLICATOR_RELOAD_HOURS", "24")))
    client = bigquery.Client(project=os.environ["BQ_PROJECT"])
    sink = BigQuerySink(client, os.environ.get("BQ_DATASET", "raw"))
    delay = 5.0
    while not stop.is_set():
        try:
            with psycopg.connect(os.environ["REPLICATOR_DATABASE_URL"], autocommit=True) as conn:
                run(Replicator(PostgresSource(conn), sink), stop, interval, reload_every)
        except Exception:
            log.exception("replication failed; retrying in %.0f s", delay)
            stop.wait(delay)
            delay = min(delay * 2, 300.0)
        else:
            delay = 5.0


if __name__ == "__main__":
    main()
