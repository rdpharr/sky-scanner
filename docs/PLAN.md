# Plan: remove the edge Postgres database

Ingest and enrichment write straight to BigQuery `raw`. The `db` and
`replicator` services, the schema, the `explorer` role, TLS, and PostGIS go
away. The edge keeps only the receiver, ingest (with a small on-disk spool)
and enrichment.

```
HOME (edge/)                                         GOOGLE CLOUD
HackRF ─▶ receiver ─▶ ingest ─▶ spool ── streaming inserts ──▶ BigQuery raw ─▶ dbt ─▶ public
                        │                                          ▲
                        ▼ heard.json (shared volume)               │
          enrichment (FAA, tar1090-db, OpenSky, plane-alert,       │
          VRS, War Watch Intel, Wikipedia, Doc 8643) ── loads ─────┘
```

## Current numbers (2026-10-19)

- Snapshots: about 6,000 a day; `raw.aircraft_snapshots` is 193 MB.
- `raw.aircraft_metadata`: 923k rows, 285 MB, max `id` 923,862.
- FAA imports happen on weekdays only (none on 2026-10-11 or 10-12). Each
  adds 115–250 versions.
- 1,423 latest FAA versions have neither `registrant_type` nor `year_mfr`.
- 5,814 aircraft with a designator were heard in the last 30 days, using
  233 designators. tar1090-db's type code covers 228 of them.

- Enrichment can't move to the cloud. On 2026-10-19 a Cloud Run job in
  us-central1 ran every enrichment download with the edge's own code. FAA,
  tar1090-db, GitHub (plane-alert, VRS), War Watch Intel and Wikipedia all
  worked. OpenSky's host (`194.209.200.34`, serving both the aircraft
  database and Doc 8643) dropped TCP connections from Google Cloud, while
  it connects in 0.5 s from home.

## Decisions

1. **Ingest streams directly.** It uses `insert_rows_json` with the same
   insert IDs the replicator uses (a hash of the key and `written_at`).
   Ingest sets `written_at` when it produces a row, and sets
   `_replicated_at` when it sends the row. The column keeps its name, so no
   table or dbt changes are needed; it now means "sent to BigQuery". Staging
   already removes duplicates.
2. **A disk spool replaces Postgres as the outage buffer.** Each batch is
   written as a JSONL file to a named volume (write to a temporary file, then
   rename), sent oldest first, and deleted once BigQuery accepts it. The cap
   is 1 GB, about 100 days at current volume; past the cap the oldest file is
   dropped and an error is logged. The receiver already holds and retries up
   to 10,000 events while ingest restarts.
3. **Metadata versions stay change-only, computed in BigQuery SQL.** Records
   are loaded into `raw._stage_metadata_<source>` with a free load job,
   which replaces any leftover staging rows; the table is dropped once the
   import commits. One multi-statement transaction then:
   - inserts new or changed versions;
   - inserts `absent` versions (full imports only);
   - inserts the `enrichment_imports` row.

   This ports `_METADATA_INSERT_CHANGED` and `_METADATA_INSERT_ABSENT`. IDs
   are `max(id) + row_number()`, which is safe with one writer. A dataset
   that was already imported is skipped by a query on `enrichment_imports`.
   `fill_added_fields` is dropped because the backfill is done (Phase 0
   checks this). We are not using dbt snapshots because they would restamp
   the existing history and can't handle tar1090-db's partial imports.
4. **Caches become append-only.** `callword_lookups` and
   `aircraft_type_images` get one streamed row per lookup, and staging keeps
   the latest row per key. The workers decide what is still fresh from
   local copies (decision 5), not by querying BigQuery.
   `military_operator_codes` is replaced with a `WRITE_TRUNCATE` load
   followed by its import row.
5. **Enrichment learns what was heard from local files, not BigQuery.**
   - Ingest keeps `heard.json` on a volume shared with enrichment. It holds
     one entry per address heard: `address_type`, first and last seen,
     and callsigns with when each was last used. (No message count:
     seeding merges BigQuery's history with what ingest heard meanwhile,
     which could count messages twice, and enrichment doesn't use it.) It
     is rewritten atomically about once a minute. Every address is kept
     (a few thousand, growing slowly); callsigns older than 30 days are
     pruned.
   - The spool files can't serve this purpose. They are deleted seconds
     after BigQuery accepts them, and the workers need up to 30 days of
     history.
   - The workers keep the reference data they download in memory: the
     tar1090-db table (already the case), the plane-alert military list and
     the military operator codes.
   - **tar1090-db** looks up addresses from `heard.json`: all of them after
     each daily refresh, otherwise those heard since its last cycle.
   - **Callwords** use a local military check over `heard.json` callsigns.
     The check uses the same rules as dbt's `military_rules`: the US
     address block, the tar1090-db flag, plane-alert and operator codes.
     (dbt's Coast Guard rule only adds evidence to aircraft already in the
     US block, so it never changes the answer.) `military_evidence` moves
     from `identity.py` to `military.py`. The edge only uses it to choose what
     to look up; dbt remains the published definition.
   - **Type photos** use tar1090-db's type code and model name for heard
     aircraft, military types first. That covers 228 of the 233
     designators dbt assigned in the last 30 days; the 5 others come from
     the FAA model mapping and would get no photo.
   - Lookup freshness (found: 30 days, not found: 7 days) comes from
     `cache/callwords.json` and `cache/type_images.json` in an enrichment
     volume, updated after each lookup.
   - Seeding: if a file is missing, it is built once from `raw` (heard
     aircraft from `aircraft_snapshots`, lookups from the two cache
     tables). A lost volume therefore costs one cheap query, not a
     re-lookup of everything.

   This needs no new permission (`sky-edge` already writes `raw`) and adds
   no hourly lag or query cost. `identity.py` is deleted except
   `military_evidence`, and `SUMMARY_SQL` and `MILITARY_CALLSIGNS_SQL` go
   with it.
6. **Doc 8643 loading** moves from the replicator to a daily
   `ReferenceWorker` in enrichment.
7. **Receivers become a dbt seed.** Ingest no longer registers itself.
8. **Enrichment logs every cycle** to a new `raw.enrichment_runs` table
   (worker, started_at, finished_at, status, summary JSON, error). This
   table replaces `aircraft_metadata` as the enrichment freshness source:
   with FAA imports on weekdays only and change-only writes, metadata can go
   days without a new row. Freshness: warn after 1 h, error after 3 h (the
   callword worker runs every 15 min).
9. **BigQuery schemas move into code** in `edge/sky_scanner/bq.py`, built
   from the replicator's `BigQuerySink`, `Column` and `to_json_value`. The
   schemas are copied from the current `raw` tables, so existing columns
   don't change. `ensure_table` creates missing tables and adds new
   columns.
10. **Query cost guard.** The only regular edge queries are the metadata
    transactions and the dataset-hash checks. Each sets
    `maximum_bytes_billed` (1 GB) and filters on `source_name`, which
    `aircraft_metadata` is clustered by. The tar1090-db worker remembers
    which addresses it has already saved for the current digest and skips
    the import when none are new.

## Phase 0: checks (read-only)

Done 2026-10-19.

- [x] Dump the current `raw` table schemas into `bq.py`: 8 table specs
      (`doc8643_types` takes its columns from its CSV header).
- [x] Confirm the 1,423 FAA versions have neither field in today's registry
      file either, so dropping `fill_added_fields` loses nothing. All 1,423
      are in today's file, and none has either field.
- [x] List the designators tar1090-db never supplies (5 of 234):
  - BALL: 1 aircraft; photo cached as not found.
  - EXEC: 1 aircraft; never looked up.
  - H900: 2 aircraft; never looked up.
  - LJ55: 1 aircraft; photo already found, and append-only keeps it.
  - S61: 1 aircraft; never looked up.

  No fallback list: EXEC, H900 and S61 have no photo today either.
- [x] Compare the local military check with `pub_aircraft.is_military` over
      30 days: 6,054 aircraft, 64 military in both, no differences.
- [x] Take a `pg_dump`: `edge/data/backup-2026-10-19.dump` (53 MB).
      Restored into a scratch database; every table's count matched.
      **Still to do: copy it off this computer.**
- [x] Confirm BigQuery holds everything in Postgres: 133,526 distinct
      snapshot keys against 133,529 in Postgres (3 arrived between the
      queries), the same first bucket, and all 63,495 capture intervals.
      `raw.aircraft_snapshots` also holds about 111k duplicate deliveries;
      staging removes them.

## Phase 1: BigQuery module and spool

Done 2026-10-19.

- [x] `bq.py`: table specs and explicit schemas for every `raw` table plus
      `enrichment_runs`. Also `ensure_table` (refuses type changes),
      `stream` (raises `RowsRejected` when BigQuery refuses rows), `load`,
      `load_csv` and `query` (with the bytes cap; `{dataset}` placeholder).
      The replicator keeps its own copy of the sink until Phase 4 deletes
      it.
- [x] `spool.py`: atomic batch files, oldest-first sending, delete on
      success, size cap. Rejected or unreadable batches are renamed to
      `*.rejected`, so they never block the queue.
- [x] `test_bq.py` and `test_spool.py`. `test_replicator.py` had no sink
      tests to move. Client tests skip without `google-cloud-bigquery`
      (CI's plain `edge` job) and run in the image job.
- [x] Live check against BigQuery:
  - Read-only, against `raw`: all 8 schemas match it exactly (types and
    modes).
  - Against a throwaway dataset: create, stream, insert-ID dedup, load,
    CSV load, parameterized queries, and `RowsRejected` on a bad row.

## Phase 2: ingest

Done 2026-10-19.

- [x] `ingest.py`:
  - The writer thread's `persist()` only spools (disk, no network) and
    updates `heard.json`.
  - A separate `Sender` thread checks the two tables once, seeds
    `heard.json` if needed, and drains the spool with backoff
    (1 s doubling to 5 min). It logs the number of batches waiting
    while BigQuery is unreachable.
  - Removed `PostgresStorage`, `init_schema` and `register_receiver`.
  - `snapshot_row` coerces typed columns, so a wrong-typed value becomes
    null instead of getting the whole batch rejected.
- [x] Interval rows: each write is a new row with its own `written_at`;
      the latest wins in staging. The receiver emits one interval event
      per turn, at its end.
- [x] `heard.py`: load, update from snapshots, prune callsigns, atomic
      save, and seed from `raw` (merged with what was heard meanwhile).
      Thread-safe. An unreadable file is set aside as `heard.json.bad` and
      rebuilt by seeding.
- [x] `test_ingest.py` (existing behavior tests kept, plus rows, heard
      tracking and the sender) and `test_heard.py`.
- [x] `bq.py`: every table, stream and query call now has a bounded retry
      (60 s) and timeout (30 s). Without that, the library's default
      10-minute retry hid outages from the sender.
- [x] Live smoke test, running the real ingest process locally against a
      throwaway dataset seeded with a copy of `raw.aircraft_snapshots`:
  1. Online: the missing table was created and `heard.json` seeded
     6,063 aircraft.
  2. Offline (dead proxy): rows spooled, and "send deferred … 5 batches
     waiting" was logged.
  3. Restart online: the spool drained.

  All 4 test snapshots and 4 intervals arrived exactly once, with typed
  columns and `fields` intact.

## Phase 3: enrichment

Done 2026-10-19.

- [x] `storage.py` is now `BigQueryStorage`, with the method names the
      workers call (`save_metadata_import`, `replace_military_operator_codes`,
      `save_callword_lookup`, `save_type_image`), plus `import_exists`,
      `record_run` and `load_reference_csv`. A full import with no records
      is refused, since it would mark every aircraft absent.
- [x] Version SQL ported to one BigQuery script:
  - Records are loaded into `raw._stage_metadata_<source>`.
  - The latest version per address is found with `QUALIFY`, and every
    value column is compared with `IS DISTINCT FROM`. A field added later
    that is null on the latest version is not a change.
  - The metadata insert and the `enrichment_imports` MERGE run in one
    transaction, after which the staging table is dropped.
  - Re-importing a dataset reuses its import row, and `changed_count`
    adds up across imports, as before.
- [x] Workers: hash dedup via `enrichment_imports`; what was heard comes
      from `heard.json` (they wait until ingest has seeded it); lookup
      freshness comes from `lookups.py` caches, which seed themselves from
      `raw`. tar1090-db skips addresses it has already saved from the
      current table, and a full pass is never lost while `heard.json` is
      missing.
- [x] `References` shares plane-alert, the operator codes and the
      tar1090-db table between workers. Switched-off sources count as
      loaded (empty), so nothing waits for them. Reference loaders run
      first.
- [x] `ReferenceWorker` loads Doc 8643 daily (moved from the replicator).
- [x] `run_forever` records every cycle (worker, times, status, summary,
      error) to `enrichment_runs`; a failure to record never stops a
      worker.
- [x] `military_evidence` moved to `military.py`. `identity.py`,
      `test_identity.py`, `us_callsign_service` and
      `is_us_military_address` are deleted.
- [x] Tests rewritten: `test_storage.py`, `test_enrich_worker.py`
      (35 tests), `test_lookups.py`, and the OpenSky worker tests in
      `test_opensky.py`.
- [x] Live check of the version SQL against copies of the real `raw`
      tables (throwaway dataset, since deleted), using today's FAA file,
      which is the one imported on 10-17 with 165 versions:
  - Re-import: 0 new versions, and the import row was reused.
  - After deleting those 165 versions from the copy, the re-import
    regenerated exactly 165 (16 of them absent), identical in every
    value column. IDs stayed unique and the staging table was dropped.
  - About 35 s per FAA import.
- [x] Live end-to-end run (`enrich_worker --once`, 94 s, with
      `heard.json` seeded from the copied snapshots):
  - Doc 8643 loaded 10,020 rows.
  - Military reference, FAA and OpenSky were skipped (same files).
  - tar1090-db checked 6,063 aircraft, matched 5,862 and found 45 changes.
  - Callwords looked up 1 word (JANET).
  - Type photos looked up 12 types, found 8. 10 of the 12 are tar1090-db
    designators that dbt doesn't use (it prefers the FAA mapping), so
    their photos are never shown; that's about a dozen extra Wikipedia
    requests a month, cached for 30 days. The other 2 (C177, PA30) are
    dbt types that had no photo before.
  - Every worker recorded its run.

## Phase 4: compose, dbt, Terraform

- [ ] `compose.yaml`: remove `db`, `replicator` and the
      `postgres-data-v1` declaration. Ingest and enrichment get `BQ_PROJECT`,
      `BQ_DATASET` and the `gcp-edge.json` mount. Ingest also gets an
      `ingest-spool` volume. A `heard` volume is writable by ingest and
      read-only for enrichment. Enrichment gets an `enrichment-cache`
      volume.
- [ ] Delete `sql/schema.sql`, `docker/`, `replicator.py`,
      `test_replicator.py` and `scripts/configure.py`. Replace the last
      with `edge/.env.example` (`RECEIVER_ID`, `HACKRF_SERIAL`,
      `BQ_PROJECT`). Remove psycopg from `requirements.txt` and `COPY sql`
      from the `Dockerfile`.
- [ ] dbt:
  - [ ] `_sources.yml`: new `raw` description, the `enrichment_runs`
        source with freshness, and remove the `aircraft_metadata`
        freshness check.
  - [ ] `stg_callword_lookups` and `stg_aircraft_type_images`: keep the
        latest row per key.
  - [ ] Add the `receivers` seed (current `raw.receivers` row) and point
        `stg_receivers` at it.
  - [ ] Update `_replicated_at` and `written_at` descriptions.
  - [ ] Delete `analyses/notable_features_prototype_postgres.sql`.
- [ ] Terraform (descriptions only; no access changes):
  - [ ] `iam.tf`: `sky-edge` becomes "Home edge: ingest and enrichment
        write raw".
  - [ ] `bigquery.tf`: new description for `raw`.
  - [ ] `alerting.tf`: alert text.
  - [ ] `jobs.tf`: freshness job description.
- [ ] `infra/check_access_tiers.sh`: add `sky-edge`, which is denied
      `analytics`, `public` and `ai`.

The dbt staging dedup works with today's data, so it can ship before
cutover. The `enrichment_runs` source ships with cutover.

## Phase 5: rehearsal against `dev_raw` (owner credentials)

- [ ] Copy the `raw` tables to `dev_raw` (table copies are free).
- [ ] **Ingest:** run the new ingest container with `BQ_DATASET=dev_raw`
      and POST fixture events.
  - [ ] Rows arrive with `written_at` and `_replicated_at` set.
  - [ ] `docker network disconnect` for 5 min: files spool. Reconnect:
        they drain, and staging over `dev_raw` has no duplicate keys.
- [ ] **FAA:** import a file Postgres already imported. Expect 0 new
      versions. Then delete that import's versions in `dev_raw` and
      re-run; expect exactly its `changed_count`.
- [ ] **Seeding:** start with empty `heard` and `enrichment-cache` volumes.
      Both are seeded from `dev_raw`, and the first callword and
      type-photo cycles look up only what `raw` doesn't already have.
- [ ] **Other workers:** `enrich_worker --once` runs every worker; check
      the `enrichment_runs` rows and bytes billed per query.
- [ ] Delete `dev_raw`.

## Phase 6: cutover (at home)

1. Stop the old `enrichment`. Restart `replicator`, which reloads every
   reference table on start, and wait for `reloaded aircraft_metadata` in
   its log.
2. Stop the old `ingest`. The receiver queues and retries. Wait two
   replicator polls (60 s), then check that `max(written_at)` of both
   streamed tables matches between Postgres and `raw`.
3. `docker compose stop replicator db`.
4. Check out the new edge, then
   `docker compose build ingest && docker compose up -d --remove-orphans`.
5. Verify:
   - [ ] Freshness is green on the next `dbt-freshness` run.
   - [ ] `enrichment_runs` rows appear.
   - [ ] `dbt-hourly` passes.
   - [ ] Data health, Aircraft and Trends pages look right.
6. Drill: repeat the 2026-10-16 alert drill, cutting the network to ingest
   instead of stopping the replicator. The alert fires, the spool drains,
   and the counts for the window match. Record it in the runbook's drill
   log.

**Rollback** (until Phase 7): check out the previous edge and
`docker compose up -d`. The Postgres volume is intact, and the replicator
resumes from BigQuery's `max(written_at)`. Its first reference reload
overwrites metadata and cache rows written by the new enrichment. Postgres
enrichment rebuilds them with different IDs, which nothing depends on.

## Phase 7: docs and cleanup

- [ ] README: architecture diagram and the `edge/` row.
- [ ] `edge/README.md`:
  - [ ] intro and services;
  - [ ] remove "Explore the database";
  - [ ] "Replication to BigQuery" becomes "Writing to BigQuery";
  - [ ] "Backup and restore" becomes a section on the spool;
  - [ ] "What has been verified live".
- [ ] `docs/runbook.md`:
  - [ ] the component table;
  - [ ] the data-stopped-arriving table and its steps;
  - [ ] deploying;
  - [ ] secrets (remove the TLS row; `gcp-edge.json` is now used by ingest
        and enrichment);
  - [ ] the recovery sections (replicator outage, edge database lost).
- [ ] Website: the `services/web/views/about.py` text, then deploy `web`.
- [ ] Two weeks after cutover:
  - [ ] `docker volume rm sky-scanner_postgres-data-v1`;
  - [ ] delete `edge/secrets/server.*` and the DB entries in `.env`;
  - [ ] drop `raw.receivers` once the seed has run.
- [ ] Delete this plan.

## Risks

- **Timing.** The portfolio deadline is 2026-10-25. This is roughly three
  working sessions. Cut over by 10-22 so the drill and docs land with
  margin; otherwise keep Postgres until after the deadline.
- **Two copies of the military rules.** The edge check (decision 5)
  repeats dbt's rules. Today `identity.py` already does this; afterwards
  it is one function. If dbt gains a rule, the edge copy only affects which
  callwords get looked up.
- **Outage durability.** It now depends on the spool volume instead of
  Postgres. Power loss can still lose the in-memory bucket being built, as
  it does today.
