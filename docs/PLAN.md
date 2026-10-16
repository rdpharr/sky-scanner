# Sky Scanner — portfolio migration plan

Written 2026-10-15. This is the working plan for moving Sky Scanner onto a
public, cloud-hosted analytics stack. It is a showcase for the
Coefficient Giving Data Scientist role, whose stack is Fivetran, BigQuery,
dbt and Streamlit. That team's stated hard problems are ambiguity, canonical
definitions, trust, permissions and AI access to data, so the plan puts its
weight there rather than on scale.

**Deadline:** apply by **Sat 2026-10-24**. Applications close Sun
2026-10-25, 11:59 pm PT, and earlier applicants may be prioritized.

## How the project maps to the role

| Role need | What the project demonstrates |
|---|---|
| Standardized metric definitions ("what counts as a renewal?") | What counts as an aircraft, a sighting, "military", "identified" or "notable"? Each has one documented, tested definition in dbt. |
| Permissions on sensitive data | Access tiers. LADD/PIA aircraft, individual owners' names and the receiver location never reach the public layer. Leak tests enforce this. |
| Context and table metadata for LLMs | dbt docs copied into BigQuery column descriptions, and a metrics catalog served by the MCP server |
| Evaluations of agent answers | Q&A and weekly-edition evals; verification rates; model comparison; before/after documentation changes |
| Run, extend and recover the platform | Freshness and quality alerts by email, a runbook and a recorded recovery drill |
| Recommendations with documented tradeoffs | Decision records; the band-allocation analysis |
| Verified figures | Checks that Postgres and BigQuery agree during migration; claim verifier for AI output |

## 1. Decisions

| Topic | Decision |
|---|---|
| Warehouse / transforms | BigQuery (US multi-region); dbt Core run as a Cloud Run Job on Cloud Scheduler |
| Ingestion | The home pipeline stays; a replicator copies Postgres to BigQuery every ~30 s. ADR 0001 explains why not Fivetran. |
| Enrichment worker | Stays at home until after the deadline (decided 2026-10-15). Moving it to Cloud Run Jobs is in §6. |
| Website | Streamlit on Cloud Run at **skyscanner.pharrhomelab.com** |
| MCP server | Python MCP SDK on Cloud Run at **mcp.skyscanner.pharrhomelab.com/mcp** (plus a local stdio mode) |
| dbt docs | GitHub Pages at **docs.skyscanner.pharrhomelab.com** |
| DNS | Cloudflare. Cloud Run CNAMEs must be **DNS-only (grey cloud)** so Google can issue the managed certificate. |
| Alerts | Google Cloud alerting → email |
| AI page | Weekly "Notable aircraft" edition. Agent model `xiaomi/mimo-v2.6-pro` via OpenRouter ($0.435 / $0.87 per million tokens, supports tool calls) |
| Public data | FAA, OpenSky, tar1090-db, plane-alert-db, VRS and War Watch Intel, all with attribution. **LADD and PIA aircraft are included and flagged** (`is_ladd`, `is_pia`; decided 2026-10-16, reversing the earlier exclusion, as public ADS-B sites do). Private owners' names are never shown; receiver location only as "northwest Las Vegas". |
| Infrastructure | Terraform from day 1; one service account per component; GitHub signs in to Google Cloud without stored keys (Workload Identity Federation) |
| Cloud region | us-central1 (supports Cloud Run custom domains) |
| CI/CD | GitHub Actions for tests and deployment only; scheduled work runs in Google Cloud |

### Open items
- Alert email address: rdpharr@gmail.com unless told otherwise.
- Grader model for evals: proposed `anthropic/claude-haiku-5.5`, from a different family than the agent.

## 2. Architecture

```
HOME (edge/)                                  GOOGLE CLOUD
receiver ─▶ ingest ─▶ Postgres ◀─ enrichment worker (FAA, OpenSky, tar1090, mil, wiki)
                         │
                   replicator ── BigQuery streaming inserts (~30 s) ──▶  raw
                                                                          │
     Cloud Scheduler ─▶ Cloud Run Job: dbt  (build hourly · freshness every 15 min)
                                                                          ▼
                                              analytics (staging → intermediate → marts)
                                                                          ▼
                                              public (views: LADD/PIA removed, private owners masked)
                                               │                 │                  ▲
                         Streamlit (Cloud Run) ◀┘                 └▶ MCP server ──────┤
                         skyscanner.pharrhomelab.com                (Cloud Run)       │
                                                                       ▲              │
     Cloud Scheduler (Mon) ─▶ Cloud Run Job: notable agent ── MCP ─────┘              │
                               (OpenRouter: MiMo-V2.6-Pro) → verifier → ai.notable_editions

     dbt job logs ─▶ log-based alert policies ─▶ email      run_results ─▶ ops tables ─▶ "Data health" page
```

## 3. Repository layout

The home-pipeline code moves into `edge/`. The Compose project keeps
`name: sky-scanner`, so the Postgres volume (`sky-scanner_postgres-data-v1`)
is unaffected. The ignored `.env` and `secrets/` move with it.

```
sky-scanner/
├── README.md                    # landing page: what it is, live links, architecture, screenshots
├── LICENSE
├── docs/
│   ├── PLAN.md                  # this file
│   ├── architecture.md
│   ├── data-sources.md          # every source, license, attribution, refresh cadence
│   ├── access-tiers.md          # who and what can see what, and how that's tested
│   ├── runbook.md               # alerts → what to do; recovery procedures; drill log
│   ├── analysis/
│   │   └── band-allocation.md   # "how should the receiver split listening time?" (if time allows)
│   └── adr/
│       ├── 0001-edge-replicator-instead-of-fivetran.md
│       ├── 0002-cloud-run-jobs-for-orchestration.md
│       ├── 0003-streamlit-for-the-website.md
│       ├── 0004-access-tiers-and-public-layer.md
│       ├── 0005-notable-aircraft-agent-design.md
│       └── 0006-cost-guardrails.md
├── edge/                        # runs at home (today's repo root, moved)
│   ├── compose.yaml             # + replicator service
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── decoder/                 # readsb patch, event header
│   ├── docker/                  # db entrypoint, init-db
│   ├── scripts/configure.py
│   ├── sql/{schema.sql, explore.sql}
│   ├── sky_scanner/             # existing package
│   │   ├── …existing modules
│   │   └── replicator.py        # NEW: incremental Postgres → BigQuery
│   ├── tests/
│   ├── README.md                # hardware, RF and local ops (moved from top README)
│   └── STATUS.md, SPEC.md       # folded into README.md and removed in Phase 7, with the LAN site
├── dbt/
│   ├── dbt_project.yml
│   ├── packages.yml             # dbt_utils, dbt_expectations
│   ├── profiles.yml             # env-var driven; maximum_bytes_billed set
│   ├── seeds/                   # squawk_codes, faa_registrant_types, aircraft_class_map
│   ├── macros/                  # dedupe, path geometry helpers, log_test_failures
│   ├── models/
│   │   ├── staging/             # _sources.yml (freshness), stg_*.sql
│   │   ├── intermediate/        # int_metadata_versions, int_aircraft_identity,
│   │   │                        # int_military_classification, int_sightings, int_sighting_paths
│   │   ├── marts/
│   │   │   ├── core/            # dim_aircraft, dim_aircraft_type, fct_snapshots, fct_sightings, agg_daily_traffic
│   │   │   ├── ops/             # fct_capture_intervals, fct_receiver_uptime_hourly, ops_dbt_runs, ops_test_results
│   │   │   └── notable/         # fct_aircraft_weekly, fct_notable_candidates_7d
│   │   ├── public/              # pub_* views, the only thing web/MCP/agent can read
│   │   └── semantic/            # semantic models + metric definitions (MetricFlow YAML)
│   └── tests/                   # custom tests: leak tests, no overlapping intervals, Postgres parity
├── jobs/                        # Cloud Run Jobs
│   ├── dbt_runner/              # runs dbt, logs one line per failure (drives alerts), loads run_results
│   │   ├── Dockerfile
│   │   └── run.py
│   └── notable/                 # weekly AI edition + evals
│       ├── Dockerfile
│       ├── agent.py             # OpenRouter tool-calling loop over MCP tools
│       ├── mcp_bridge.py        # MCP tool schemas → OpenAI-style tool schemas
│       ├── verifier.py          # re-checks every claim against BigQuery
│       ├── prompts/
│       ├── evals/
│       │   ├── qa_questions.yaml   # ~25 questions with correct-answer SQL
│       │   ├── run_qa_eval.py
│       │   └── run_edition_eval.py # verification rate + grader rubric, compares models
│       └── tests/
├── services/
│   ├── web/                     # Streamlit
│   │   ├── Dockerfile
│   │   ├── app.py
│   │   ├── pages/               # live, trends, aircraft log, notable, definitions, data health, about
│   │   ├── lib/                 # BigQuery client, caching, map helpers
│   │   └── tests/
│   └── mcp/
│       ├── Dockerfile
│       ├── server.py            # Streamable HTTP + stdio
│       ├── tools/               # aircraft, flights, types, definitions, quality, sql
│       └── tests/
├── infra/
│   └── terraform/               # project services, datasets, IAM, Artifact Registry, Secret Manager,
│                                # Cloud Run services/jobs, Scheduler, domain mappings, alert policies,
│                                # email channel, budget, quotas, GitHub sign-in setup
└── .github/workflows/
    ├── ci.yml                   # pytest (edge, web, mcp, jobs), ruff, terraform validate, gitleaks
    ├── dbt-ci.yml               # on PR: build only changed models into a pr_<n> dataset, then drop it
    ├── deploy.yml               # on main: build images → Artifact Registry → deploy Cloud Run
    └── dbt-docs.yml             # on main: dbt docs generate → GitHub Pages
```

## 4. Components

### 4.1 Edge and replicator
- **`edge/sky_scanner/replicator.py`** (Compose service `replicator`):
  - **Snapshots and capture intervals** are polled every 30 s by a
    `written_at` column the edge schema maintains, and sent with BigQuery
    streaming inserts. Each poll re-reads a 2-minute overlap so late
    commits are not missed; rows already sent are skipped. Possible
    duplicates (for example after a restart) are removed in dbt staging.
  - `written_at` rather than `bucket_start`: ingest retries snapshots
    after a database outage, so late rows can carry old bucket times.
  - **Reference tables** (metadata, imports, military codes, callword
    lookups, type images, receivers) are reloaded in full daily with free
    load jobs.
  - **Backfill:** an empty BigQuery table is filled by one load job; then
    streaming resumes from what BigQuery holds.
  - Columns come from the Postgres catalog, so new edge columns appear
    automatically. **`receivers.location` is never replicated.**
  - Streaming inserts cost $0.01 per 200 MB, a few cents a year at this
    volume. They are simpler than the Storage Write API.
  - A failing table is retried on the next poll; repeated failures
    reconnect and start over.
- **Credentials:** a key for `sky-edge` (writes `raw` only) in
  `edge/secrets/gcp-edge.json`, outside Terraform state. It is the only key
  in the system; ADR 0006 explains why. Postgres access uses the read-only
  `explorer` role.
- **Enrichment change (pending):** import the FAA `type_registrant` and
  `year_mfr` fields, filled in on existing rows without creating new
  registry versions. They drive owner categories and aircraft age.

### 4.2 BigQuery
| Dataset | Contents | Who can read it |
|---|---|---|
| `raw` | Replicated tables, unchanged | dbt only |
| `analytics` | Staging, intermediate, marts, ops | dbt, owner |
| `ai` | `notable_editions`, agent traces, eval results | notable job (writes), dbt (reads) |
| `public` | `pub_*` views | web, MCP, notable agent |

- **Table design:** snapshot facts are partitioned by `DATE(bucket_start)`
  and clustered by `address`. `fields` → `JSON`, `position` → `GEOGRAPHY`.
- **Column documentation:** dbt descriptions are copied into BigQuery, so
  agents see them.

### 4.3 dbt: canonical definitions
Each definition is one model with documentation, tests and a metric:
- **Confirmed aircraft:** an address heard in more than one message.
- **Sighting:** consecutive snapshots of one aircraft with no gap over 10
  minutes.
- **Identified:** a match in FAA, then tar1090-db, then OpenSky, then
  plane-alert-db, in that order. The source that matched is recorded.
- **Military:** any of the five rules, recording which rule fired.
- **Receiver uptime:** listening time ÷ wall-clock time, per band.
- **Notable score:** frequency, ownership, flight-path and type components
  (§4.7).

**Metrics:** confirmed_aircraft, sightings, military_sightings,
identification_rate, receiver_uptime_pct, messages_per_listening_hour, plus
the notable score components.

### 4.4 Data quality and email alerts
| Check | Severity |
|---|---|
| Source freshness: snapshots error after 30 min (this is the "receiver down" alarm); metadata error after 3 days | error |
| Keys unique and not null; related keys match | error |
| Value ranges: lat/lon, altitude −1,500 to 60,000 ft, ground speed ≤ 800 kt, valid squawk format | warn |
| Capture intervals do not overlap; every snapshot falls inside a listening interval for its band | error |
| Hourly uptime ≥ 80% | warn |
| Row-count anomaly: today vs the same weekday over the last 4 weeks | warn |
| Share of unconfirmed addresses / identification rate outside normal bounds | warn |
| **Leak tests:** no LADD/PIA aircraft, no individual owners' names and no receiver location in `public` | error |
| Postgres vs BigQuery parity (migration period) | error |

- **What triggers an email:**
  - `dbt_runner` logs one line per error-level failure; a log-based alert
    policy emails it.
  - Any Cloud Run Job execution failing (dbt, notable).
  - The budget reaching 50/90/100%.
- **Where warnings go:** only to the Data health page, so the inbox stays
  quiet.

### 4.5 Access tiers
- **Owner:** everything.
- **Public service accounts** (web, MCP, agent): `public` only, with a
  per-account BigQuery daily usage quota as a hard spending ceiling, plus
  `maximum_bytes_billed` on every query.
- **In CI:** a test checks that the public account cannot read `analytics`.

### 4.6 Website (Streamlit)
| Page | Contents |
|---|---|
| **Live** | pydeck map of aircraft heard in the last 2 min, refreshing every 15 s while open; counts by band |
| **Trends** | Daily traffic, uptime-normalized rates, hour × weekday heatmap, identification rate and aircraft mix over time (Phase 7) |
| **Aircraft log** | The current LAN page rebuilt: filters, totals by class/type; detail view with identity from every source, sightings, track, altitude/speed profile, type photo, military panel |
| **Notable this week** | The AI edition: verified badges, track maps, "how this was made" (tool calls, model, tokens, cost), past editions |
| **Definitions** | Metric glossary generated from the dbt metric definitions |
| **Data health** | Freshness, test results, uptime, recent dbt runs, link to dbt docs |
| **About** | Architecture, decision records, data sources and attribution, how to connect to the MCP server |

- **Hosting:** Cloud Run with one instance always on during the application
  period, to avoid Streamlit's slow first load.
- **Caching:** query results are cached for 15–60 s depending on the page.

### 4.7 MCP server
| Tools | Notes |
|---|---|
| `get_definitions`, `describe_dataset` | Come from the dbt manifest: metric definitions plus table and column descriptions |
| `search_aircraft`, `get_aircraft`, `get_sightings`, `get_flight_summary`, `get_type_info`, `get_history` | Flight summaries describe the path (circling score, altitude band, area covered), not raw points |
| `recent_activity`, `traffic_stats`, `military_sightings`, `get_notable_candidates` | |
| `receiver_status`, `data_quality_status` | |
| `run_sql` | Needs a token; `public` dataset only; each query is dry-run first and refused above a byte cap |

- **Safety:** read-only, rate-limited, and tested with the MCP Inspector
  tool.

### 4.8 Notable aircraft agent (weekly, Mondays 06:00 PT)
1. **Candidate scores** come from `fct_notable_candidates_7d`:
   - **Frequency:** sightings vs the aircraft's own history; days seen;
     first-ever sighting; returned after an absence.
   - **Ownership:** registrant category (government, LLC, corporation,
     individual, shown only as "private"); military operator; foreign
     registration; tar1090-db "interesting" flag.
   - **Flight path:** circling ratio and total turning; time at low
     altitude; unusual altitude or speed for the type; emergency squawks.
   - **Type:** how rare the type is here and in the FAA registry; first time
     the type was seen; military or historic; aircraft age.
2. **The agent** (MiMo-V2.6-Pro through OpenRouter) connects to the
   **public MCP endpoint**. It investigates the top ~25 candidates, picks
   5–8, and writes structured entries. Each claim cites the tool call it came
   from.
3. **The verifier** re-checks every number, date and fact against
   BigQuery. Failing claims are removed; entries without enough support are
   dropped.
4. **Output:** the edition, the full trace, token count and cost are written
   to `ai.notable_editions` and exposed through a `public` view.
5. **Evals (same code):**
   - **Q&A eval:** ~25 questions with correct-answer SQL, run against a
     fixed "golden week" so results are repeatable.
   - **Edition eval:** verification rate plus a rubric score from the
     grader model.
   - **Comparison:** run several models on demand.
   - Results feed the site and a before/after showing how better
     documentation changed accuracy.
6. **Secrets:** a dedicated OpenRouter key with a credit limit, stored in
   Secret Manager. Prompt rules: no guessing at hidden ownership or who is
   aboard, and hedged language for anything inferred.

#### Prototype findings (2026-10-15, Postgres, last 7 days)
The features were prototyped on live data before being ported to dbt. The
week held 2,363 confirmed aircraft and 3,604 sightings.

| Finding | Design change |
|---|---|
| **Raw circling and turning is dominated by flight-school Cessna 172s doing routine pattern work.** One police helicopter was the only standout in the top 25. | Score path features **relative to the aircraft's own prior 28 days and its owner's fleet**. Mark owners seen on most days as routine and down-weight them. |
| **64% of sightings have no position**, so path features cover only about a third of traffic. | A path component is null (not zero) below a minimum number of positioned snapshots. Missing positions never lower a score. |
| **45% of this week's aircraft are "first ever"**, because collection only began on Sep 26. "Returned after an absence" matched nothing. | Gate aircraft-level novelty signals until there are ≥ 8 weeks of history (about Nov 21). Until then, use type-level novelty and FAA fleet rarity. |
| **Type rarity is the strongest signal.** Types seen on only 1–2 airframes surfaced the Air Force VC-25A 92-9000, an F/A-18F Super Hornet and a K-MAX helicopter. | Type rarity carries the most weight for now. Add FAA registered-fleet counts so rare-here and rare-nationally can be told apart. |
| **About 10% of the week's aircraft are LADD/PIA** (232 of 2,363). The K-MAX was LADD-flagged. | Included everywhere public and flagged (`is_ladd`, `is_pia`); the agent must say so when it mentions one. (Originally removed; changed by the owner's decision in Phase 6.) |
| tar1090-db's "interesting" flag never appears in this data. | Drop it as a feature. |
| OpenSky lists American Airlines as the operator of a privately registered PA-27 Aztec, which is stale or wrong. | Operator and owner values are **attributed claims**, resolved by source precedence. The agent must name its source, and the verifier checks that source. |
| Government-looking FAA owners (police, county, federal, university) match 18 aircraft a week by name pattern. | Use name patterns until the FAA `type_registrant` field is imported, then switch to the registrant category. |
| No emergency squawks this week. | Keep the feature; expect it rarely. |

### 4.9 CI/CD and operations
- **CI** (`ci.yml`, `dbt-ci.yml`): tests, linting, gitleaks, plus a dbt
  build of only the changed models on every pull request.
- **Deploy:** on merge to main, build images and deploy them to Cloud Run.
- **Two CI identities, least privilege:** `sky-ci-plan` (any branch of this
  repository) can only read: `terraform plan` and dbt CI. `sky-ci-deploy`
  (main branch only) can push images and update Cloud Run, but cannot change
  IAM or datasets. `terraform apply` is run by the owner, so no automated
  identity can grant itself access.
- **Runbook:** one entry per alert. Includes rebuilding marts from `raw`,
  re-running a backfill, rotating the replicator key and re-running a
  notable edition.
- **Recovery drill:** unplug the antenna and record the timeline: freshness
  alert email → recovery → a check that the data balances again.

### 4.10 Cost (monthly estimate)
| Item | Cost |
|---|---|
| BigQuery storage + queries (free tier: 10 GB, 1 TB) | ~$0 |
| Cloud Run: Streamlit with 1 instance always on | ~$5–10 |
| Cloud Run: MCP, jobs (scale to zero) | ~$0–1 |
| OpenRouter: 4 editions + evals | ~$1–3 |
| Secret Manager, Scheduler, Artifact Registry | ~$0–1 |
| **Total** | **~$10–15** (budget alert set at $25) |

## 5. Phases

The work runs in phases, in dependency order. **Deadline: apply by Sat
2026-10-24**, so every phase below must be finished by then except where
marked optional. Phases 4 and 5 can overlap once Phase 2 is done. Phase 7
(polish and cleanup) finishes before Phase 8 makes the repository public.

**Demonstrations.** Each phase ends with a demo on the live services and
real collected data, never fixtures or mocks. Demos happen as soon as each
capability exists rather than at the end. Claude runs them and shows the
actual queries, outputs and costs (bytes billed), and anything that
doesn't hold up becomes a fix before the phase closes.

### Phase 0: Foundations — done
- gcloud and Terraform installed; project `sky-scanner-pharr` bootstrapped
  (billing, APIs, $25 budget, Terraform state bucket).
- Home pipeline moved into `edge/`; Compose stack recreated with the same
  Postgres volume.
- Terraform core applied: datasets, service accounts and permissions,
  GitHub sign-in, Artifact Registry, OpenRouter secret, email channel and
  a 100 GiB per-account daily query quota (granted).
- Notable-scoring prototype on Postgres (findings in §4.8).

**Demo (done):** the prototype's real results. Rare types surfaced the
VC-25A 92-9000, an F/A-18F and a K-MAX (LADD-flagged, so excluded from
public). Raw circling turned out to be dominated by flight schools.

### Phase 1: Data lands in BigQuery — done (2026-10-16)
- `replicator.py` and its Compose service: snapshots and capture intervals
  streamed every 30 s by `written_at`; metadata and reference tables
  reloaded daily; `receivers.location` never replicated.
- Key for `sky-edge` created locally into `edge/secrets/`, never in
  Terraform state.
- Full history backfilled by load job on first start (NDJSON, not Parquet).
- FAA `registrant_type` and `year_mfr` added to the edge importer. Existing
  rows are filled at the next FAA publication. A dry run on the live table
  filled all 317,604 current FAA aircraft and created 0 new versions.

**Demo results (live):**
- Row counts: all six reference tables match Postgres exactly
  (`aircraft_metadata` 922,452). The streamed tables trailed by 1 snapshot
  and 2 intervals written seconds earlier. No duplicate keys.
- Freshness: newest capture interval **0 s behind Postgres** at steady
  state.
- Real queries: aircraft in the last 3 hours; the VC-25A 92-9000 heard
  Wed Oct 14 19:38 PT at 6,375–6,775 ft (3 snapshots); busiest local hours
  are 08:00–13:00 PT (~9,800 messages a day at 09:00); 978 MHz saw at most
  3 aircraft in any hour.
- Cost: small queries bill the 10 MB minimum. Joining raw
  `aircraft_metadata` billed 44 MB, so dbt's current-identity dimension
  needs to stay small.

**Fixed during the demo:**
- Backfilled rows inside the 2-minute overlap were re-streamed, because a
  schema migration stamped every existing row with one `written_at`. The
  backfill now records them as sent.
- A streaming error restarted everything, reloading 922k metadata rows.
  Failed tables are now retried on the next poll, with a full restart only
  after 5 failing polls. The trigger was BigQuery's streaming endpoint
  reporting "not found" for a few minutes after a table was recreated.

### Phase 2: dbt models and canonical definitions — done (2026-10-16)
- dbt Core 1.12.6 + dbt-bigquery 1.12.1 in `dbt/.venv`. Targets: `dev`
  builds into `dev_analytics`/`dev_public`; `prod` into `analytics`/`public`.
- 10 staging views, 3 intermediate tables (version history, identity
  resolution, military classification), 7 marts (`fct_snapshots`
  incremental, `fct_sightings` with path features, `dim_aircraft`,
  `dim_aircraft_type`, `agg_daily_traffic`, `fct_capture_intervals`,
  `fct_receiver_uptime_hourly`), 7 `pub_*` views.
- 7 seeds generated from `aircraft_log.py` and reference lists (type names,
  FAA model patterns, airline prefixes, registrant types, squawks, local
  airports).
- 11 MetricFlow metrics on 4 semantic models (`models/semantic/`).
- The replicator now also loads the ICAO Doc 8643 type list
  (`raw.doc8643_types`), keeping file order so the first entry wins as in
  Python.
- Notable-candidate scoring is deferred to Phase 6.

**Demo results (live, prod):**
- `dbt build`: 72 pass, 1 warning. The warning is 8 snapshots with
  impossible altitudes (a DA40 at 100,300 ft, an E175 at 120,400 ft):
  decoding errors the test is meant to surface.
- **Parity with the LAN page, all 5,474 aircraft:** 0 differences in
  confirmed status, military, kind, class, designator and registration. 10
  type names differ only in capitalization ("Van's" vs Python's "Van'S").
  One identity-source difference is a LAN list-page bug: N425FX (A50EC5) was
  deregistered by the FAA on Oct 14, but the list page still credits the
  FAA. dbt and the LAN detail page both use the latest version.
- Definitions:
  - 5,173 confirmed aircraft; 304 single-message addresses (5.6%) excluded.
  - 12,145 sightings, 2.35 per aircraft; median 2.2 min, longest 104 min;
    6,400 with positions.
  - Identification: FAA 90.2%, tar1090-db 9.5%, OpenSky 0.1%,
    unidentified 0.3%.
  - 59 military aircraft. tar1090-db flags all 59; 45 are in the US military
    block; 35 are on plane-alert-db; 4 use military operator codes; 1 uses
    the Coast Guard callsign convention. 10 are flagged military only by
    tar1090-db: an open definitional question.
- Leak tests pass on `public`. Pointed at `analytics.dim_aircraft`, the
  LADD/PIA test would fail with 599 rows (585 LADD, 14 PIA); public has
  4,878 of 5,477 aircraft.
- Owner names: 293 organization names shown; 4,043 withheld. All FAA
  owners stay withheld until the registrant type arrives with the next FAA
  publication.
- Descriptions reach BigQuery (`persist_docs`).

**Follow-up for Phase 5:** some `pub_*` columns lack descriptions because
they are documented only on the underlying mart (e.g.
`pub_aircraft.aircraft_class`). Share column docs with doc blocks and add a
check that every public column is described.

### Phase 3: Scheduling, data quality and alerting — done (2026-10-16)
- `jobs/dbt_runner/run.py` in one image, three Cloud Run jobs:
  - `dbt-hourly` (:05 past each hour): build excluding models tagged `daily`.
  - `dbt-daily` (06:35 UTC): build everything.
  - `dbt-freshness` (every 15 min): source freshness.
- Cloud Scheduler starts the jobs as `sky-scheduler`, which may only invoke
  them.
- The runner logs one JSON line per failed check (ERROR) or warning
  (WARNING). It saves every result to `analytics.dbt_invocations` and
  `analytics.dbt_node_results`, and exports metric, model and column
  definitions to `analytics.dbt_definitions`. It fails the job only if dbt
  cannot run, so one problem sends one alert.
- Four email alert policies (`infra/terraform/alerting.tf`): data check
  failed, data stopped arriving, scheduled job failed, freshness checks
  stopped running. Warnings are not emailed; they appear on the Data health
  page.
- Cost work: `fct_snapshots` merges only the last 2 days of partitions, with
  the cutoff computed first so BigQuery can prune (190 MB → ~20 MB). The
  900k-row version history moved to the daily build.
- Measured cost: hourly build ~700 MB, freshness 31 MB, daily ~1.1 GB, about
  610 GB/month of the 1 TB free tier. Most of it is the 10 MB minimum across
  ~40 tests. Before launch, consider running tests every 2–3 hours or
  making sightings incremental, since full-history models grow linearly.

**Demo results (live):**
- All three jobs succeeded on Cloud Run: freshness 3 nodes, hourly 73,
  daily 79, 0 errors. Each run took about 3–5 minutes, of which dbt itself
  took 28–55 s; the rest is container start-up.
- **Alert drill (2026-10-16 UTC):**

  | Time | Event |
  |---|---|
  | 02:24:03 | Newest capture interval reached BigQuery |
  | 02:24:28 | Replicator stopped (receiving and Postgres kept running) |
  | 02:31:46 | Freshness: `capture_intervals` 8 min old → warning, no email (as designed) |
  | 02:46:23 | Freshness: `capture_intervals` 22 min old → error logged |
  | 02:46:59 | Email "Sky Scanner: no new raw.capture_intervals data" received (36 s after the error) |
  | 02:49:12 | Replicator restarted |
  | ~02:51 | Caught up: all 46 snapshots from the outage in BigQuery, matching Postgres |
  | 02:54:24 | Freshness: 0 errors, 0 warnings |

- Follow-ups: give alert policies a severity (emails say "No severity").
  The alert email lands in Gmail's Updates tab.

### Phase 4: Website (Streamlit) — done (2026-10-16)
- `services/web` (Streamlit 1.66) on Cloud Run service `web` as `sky-web`.
  One warm instance, CPU throttled when idle, session affinity for
  websockets.
- Pages: Live, Aircraft log (with per-aircraft detail), Definitions, Data
  health, About.
- **Live data:** `pub_live_snapshots` reads the last 30 minutes of raw data
  directly, so the map is seconds old rather than up to an hour. LADD/PIA
  aircraft are excluded via `int_privacy_hidden_addresses`, which lists
  every tar1090-flagged address, heard or not. The `public` dataset is
  authorized on `raw` for this view.
- **Cost guards:** every query has `maximum_bytes_billed` (500 MB) and is
  cached for 15 s to 1 h. The live map stops polling after 10 minutes
  without interaction.
- **Definitions:** the runner exports dbt's metric, model and column
  definitions to `analytics.dbt_definitions`; the site reads them through
  `pub_definitions`.
- **Data health:** reads `pub_data_health` and `pub_dbt_runs` (views, so
  each freshness run shows within 5 minutes) and receiver uptime.
- **Domain:** Cloud Run domain mapping (Terraform). The Cloudflare CNAME
  `skyscanner` → `ghs.googlehosted.com` (DNS only) was created through the
  Cloudflare API with a token scoped to DNS edits on this zone, stored
  outside the repo in `~/.config/sky-scanner/cloudflare-token`. Move the
  record into Terraform in Phase 8.
- **Owner impersonation:** the owner holds token-creator on `sky-web`,
  `sky-mcp` and `sky-notable` so the access tiers can be tested as those
  accounts. `infra/check_access_tiers.sh` does this.

**Demo results (live):**
- The site serves from `https://web-5hakmcrcrq-uc.a.run.app` in about
  0.14 s. Through `sky-web`, the live map shows aircraft from raw data.
- **Detail page vs SQL:** on the VC-25A 92-9000, all 12 fields and the
  sighting count match a hand-written query.
- **A bug the demo caught:** the detail page crashed on missing years
  because BigQuery INT64 nulls arrive as `pd.NA`. Fixed, with
  `services/web/tests/test_fmt.py`.
- **Access tiers** (`infra/check_access_tiers.sh`): `sky-web`, `sky-mcp`
  and `sky-notable` can read `public.pub_aircraft` and
  `public.pub_live_snapshots`. They are refused `analytics.dim_aircraft`
  (owner names, LADD) and raw tables read directly.
- **Cost per query:** 0.2–0.6 s each. The live map bills 31.5 MB per
  refresh (three tables at the 10 MB minimum), so one 10-minute session
  costs up to ~1.3 GB. Other pages bill 5–21 MB per query and are cached.

**For Phase 7:**
- Prefer the more specific military type name (tar1090-db's "Boeing
  VC-25A" over Doc 8643's "Boeing 747-200"; the LAN page has the same
  issue).
- Narrow metric labels; the class chart's truncated labels.
- Load the `developing-with-streamlit` skill before UI work.

### Phase 5: MCP server — done (2026-10-16)
- **Agent context first:** public column descriptions in BigQuery went from
  3 of 159 to **159 of 159**. `dbt/scripts/column_docs.py` holds one
  meaning per column and generates the doc blocks (`models/public/_columns.md`)
  and the public YAML, with per-view overrides where a name means something
  different (e.g. `snapshots`: lifetime on aircraft, per day on daily
  traffic). The new test `public_columns_are_documented` fails the build if a
  public column in BigQuery has no description.
- **`services/mcp/server.py`** (MCP Python SDK 2.3, `MCPServer`), stateless
  Streamable HTTP on Cloud Run service `mcp` as `sky-mcp`; stdio locally.
  Server instructions tell agents to use the canonical definitions, cite
  sources, say which time zone they use, and never guess private owners or
  occupants.
- 14 tools, all annotated read-only: `get_definitions`,
  `describe_dataset`, `search_aircraft`, `get_aircraft`, `get_sightings`,
  `get_flight_summary`, `get_type_info`, `get_history`, `recent_activity`,
  `traffic_stats`, `military_sightings`, `receiver_status`,
  `data_quality_status`, `run_sql`.
- **`run_sql`** needs the `X-Sky-Scanner-Token` header (secret
  `mcp-sql-token`, generated by Terraform). It dry-runs first and refuses
  non-SELECT statements, other projects (billed to us) and scans over 1 GB.
  A dry run lists the tables *under* a view, so the server cannot tell
  public from private by name: **IAM is the boundary** (`sky-mcp` reads only
  the public views).
- Other guards: per-query byte cap of 300 MB, 120 tool calls per minute per
  instance, parameterized SQL, clamped arguments. Cloud Run reserves
  `/healthz`, so the health route is `/health`.
- `services/mcp/tests`: 10 unit tests (SQL checks, token requirement,
  rate limit, all tools read-only).
- DNS: `mcp.skyscanner` CNAME created through the Cloudflare API.
- Deferred: dbt docs on GitHub Pages (needs the repository public; moved to
  Phase 8). The notable-candidates tool comes in Phase 6.

**Demo results (live, `https://mcp-5hakmcrcrq-uc.a.run.app/mcp`):**
- All 13 data tools answer through the SDK client.
- **Answers vs independent SQL on the private marts, all match:**
  - "What counts as a sighting?" returns the dbt definition.
  - "Military aircraft in the last 7 days": 26 aircraft, 103 sightings,
    the same in SQL (top types: C-17A ×6, JANET 737-600 ×5).
  - 1090 MHz on Oct 14 and 15: 356 and 360 aircraft, the same in SQL.
  - N8930S: Southwest 737 MAX 8, identified by the FAA, matches.
- **Privacy:**
  - Owners of privately registered aircraft come back as
    `owner_name: null`, `owner_category: "Private or unverified"`.
  - A Cessna 185 seen last week does not exist through MCP at all: it is
    on the FAA LADD list.
- **`run_sql` as `sky-mcp` in production:**
  - No token or the wrong token: refused.
  - Public views: allowed (3,303 airline, 1,247 other, 59 military).
  - `analytics.dim_aircraft` and raw tables: **BigQuery: Access Denied**.
  - Another project: refused by the server.
  - CREATE and DELETE: refused by BigQuery permissions.
- Custom domains: `https://skyscanner.pharrhomelab.com` and
  `https://mcp.skyscanner.pharrhomelab.com/mcp` are live with Google-managed
  certificates (issued 15:21 UTC). The website's first attempt failed
  because the mapping was created before its DNS record, so it took ~48
  minutes; Google retried on its own.

**For Phase 7:** Doc 8643's first entry for C185 is "Aviatech Super
Cyclone". Add curated names for common GA types (C185 "Cessna 185
Skywagon") and prefer tar1090-db's model for military variants.

### Phase 6: Notable aircraft agent and evals — in progress (2026-10-16)
**Built:**
- **Template:** the owner's reference report "Notable aircraft over
  northwest Las Vegas, 26 Sep to 2 Oct 2026" (Google Doc) is the template
  for the page and the gold standard for edition recall.
- **Candidates:** `fct_notable_candidates`, one candidate set per 7-day
  window, scored from rarity as of the window, military, vintage civil,
  civil government, unusual path, frequency and foreign registration.
  `pub_notable_candidates`.
- **MCP tools for the edition:** `week_summary`, `callsign_activity`,
  `operator_fleets`, `busiest_areas`, and date ranges on every time-based
  tool. Output measurements are rounded (coordinates are not).
- **`jobs/notable`:** an OpenRouter agent (MiMo-V2.6-Pro, low reasoning
  effort) investigates through the public MCP endpoint and writes an
  outline citing call ids. Then it writes the front matter, each section
  intro and each entry in parallel (6 at a time) from only the cited
  results. Small parts removed output truncation, and a failed part costs
  one entry, not a section.
- **The verifier:** every statement must cite tool calls. Each cited value
  must match the call's real output (rounding only) and appear in the
  sentence (digits, number words or a close paraphrase). Entry titles must
  contain the data's type name or they are replaced. Failures are removed
  and listed.
- **Storage and pages:** editions go to `ai.notable_editions` (verified
  edition, draft, outline, report, photos with Commons credits, full trace,
  tokens, cost), shown through `pub_notable_editions` on the Notable page
  ("Show evidence", "How this edition was made"). Evals go to
  `ai.eval_runs` → `pub_eval_runs` → the Agent evals page.
- **Weekly job:** Cloud Run job `notable-edition`, Mondays 06:00 Pacific
  (Terraform pending apply).

**Findings that changed the design:**
- **Verifier strictness:** its first version removed 44% of statements.
  Most were correct sentences written with number words, paraphrases or raw
  decimals. Fixing the verifier and having tools round their output took
  the same draft to 86% and new drafts to 81%. The real catches that remain
  include a speed from the wrong row and an invented field.
- **Speed:** an edition takes ~14 min and $0.19. The time is the
  provider's ~34 tokens/s plus ~8 s fixed latency per request, not the
  tools (77 s). Ultraspeed is 3x faster at ~20x the cost; Pro stays for the
  unattended weekly job.
- **Recall against the reference week was 60% (15/25).** Military 12/14,
  but rare and vintage civil aircraft 0/6. Scoring ranked any military
  aircraft above a type heard only once, so the DC-9, OV-10 and L-39s fell
  outside the candidates the agent read. Reweighted (rarity 0.45, military
  0.20, new vintage signal) and separate military and civil candidate
  lists.
- **Q&A eval (15 questions on the reference week, graded against SQL):**
  Claude Haiku 5.5 15/15 in 28 s for $0.024; MiMo-V2.6-Pro 10/15 in 104 s
  for $0.046. All five MiMo misses were weekly totals: given "26 September
  to 2 October" it passed end_date 2 October to tools whose end date was
  exclusive. Tools now take inclusive dates. **After: MiMo 15/15** (84 s,
  $0.040); Haiku 14/15 (it miscounted distinct types from a list it had
  fetched correctly).
- **Writing a section per request failed on big sections.** After the
  reweight, three of six sections came back cut off or without valid JSON
  (a 9-entry military section hit the output limit), and recall fell to
  32%. Writing each entry in its own request fixed it.

**Recall:**

| Edition | Change | Recall | Verified | Entries | Time | Cost |
|---|---|---|---|---|---|---|
| `b534c3ae` | first full edition | 15/25 (60%) | 81% | 17 | 14 min | $0.19 |
| `9c32d198` | reweighted candidates, a section per request | 8/25 (32%) | 77% | 8 | 19 min | $0.22 |
| `5d02d041` | an entry per request, inclusive dates | **19/25 (76%)** | 81% | 20 | 7.6 min | $0.28 |

Still missed: C-37B, UH-60s, L-39s, the civilian S-70, the MGM Resorts
E190 and the TransMedics Phenom 300s (corporate fleets).

**Left:** `terraform apply` for the weekly job; first published edition.

### Phase 7: Website polish and repository cleanup — in progress (2026-10-16)
**Done so far:**
- **Trends page** from two new marts (`agg_hourly_traffic`, `agg_daily_mix`):
  last-7-days tiles with daily bars and rich tooltips, daily aircraft by kind
  with low-uptime days shaded, aircraft per listening hour, a weekday × hour
  heatmap, class mix, identification by source, per-band rates. Every chart
  has a data table and links to its definition. Headline checked by hand
  (433.9 vs 434).
- **Look:** "sectional chart" theme in `.streamlit/config.toml` (chart-paper
  white, airway-magenta accent, airspace-blue links, Barlow Semi Condensed
  headings, Public Sans body, matching dark mode); chart palettes checked
  with the dataviz validator in both modes. Live opens with the live count;
  prose pages use the centered layout; Material icons; alt text on charts
  and tables; production hides error details and the developer menu.
- **Navigation:** reference pages under a Methods section; every aircraft
  has its own page (`/aircraft-detail?address=…`) linked from the Aircraft
  log, Live and Notable.
- **Notable tuning:** Wikipedia search hits must name the same model (18
  wrong articles cleared); ISO timestamps stripped before verification;
  Commons credits trimmed to the author. LLC owners stay named (owner's
  decision).

- **Repository cleanup:**
  - LAN website removed (`web.py`, `aircraft_log.html`, the `web` Compose
    service, its tests); the worker's identity code kept as
    `edge/sky_scanner/identity.py`. `demo.py` and `sql/explore.sql` removed.
    `edge/SPEC.md` and `edge/STATUS.md` folded into `edge/README.md`.
  - ruff (format and lint, `pyproject.toml`), sqlfluff (BigQuery dialect,
    jinja templater with lint-time stubs for `dbt_utils`, `target` and
    `run_query`, so CI needs no warehouse) and `terraform fmt`, pinned in
    `requirements-lint.txt` and run in CI with tests for web, MCP and the
    notable job. Linting found a real bug: two columns shared one doc
    block, so `pub_definitions.model` was described as the edition's model.
  - SQL reformatting verified: compiled SQL identical before and after
    except five intended edits; all 86 models and tests dry-run on
    BigQuery; a production build passed. (The capitalisation rule had
    lowercased `INFORMATION_SCHEMA`, which BigQuery rejects; caught by the
    dry runs and restored.)
  - New top-level README with live links, screenshots and architecture;
    MIT license; GitHub description, website and topics set;
    `docs/images/social-preview.png` ready to upload (Settings → Social
    preview).

**Left:** the design-plugin review pass (`design-critique`,
`accessibility-review`, `ux-copy`) once its skills are available here; a
busier-hour screenshot of the Live page.

Comes before launch: the repository goes public and the application goes
in only after this phase. It starts once Phases 4–6 have pages and the LAN page is no longer needed
for the Phase 2 parity check.

- **Trends page** (new Streamlit page, from `agg_daily_traffic` and the ops
  marts):
  - Daily confirmed aircraft, sightings and military sightings, by band.
  - Rates normalized by listening time (aircraft and messages per listening
    hour), shown alongside raw counts, so receiver downtime is not read as a
    drop in traffic. Hours below the 80% uptime threshold are shaded.
  - Hour-of-day × day-of-week heatmap; identification rate by source over
    time; aircraft mix by class over time.
  - Week-over-week comparisons only for complete weeks. Collection began
    Sep 26, so the page says how much history it covers rather than
    implying long-run trends.
  - Every chart links to its metric in the Definitions page.
- **UI/UX pass** across every page. Presentation matters more than search
  ranking, so SEO is out of scope. Skills, each with one job:
  - **`frontend-design`** (Anthropic) sets the visual direction: palette,
    type, tone and the landing page's first impression.
  - **`developing-with-streamlit`** (Streamlit's official skill, bundled
    with the pinned `streamlit==1.66.0`) implements it with native
    elements: `config.toml` theme tokens, `st.navigation`, bordered metric
    cards, fragments and skeletons for loading, `alt=` on charts.
    `frontend-design` favours custom CSS; Streamlit's skill forbids it
    because selectors break on upgrade. Resolve by expressing the design
    as theme tokens, allowing CSS only in one deliberate, isolated place
    (e.g. the landing header), and keeping the Streamlit version pinned.
  - **`dataviz`** for every chart, especially the Trends page: chart
    choice, the palette validator, light and dark mode, stat tiles.
  - **Design plugin** (Anthropic) as the review pass before launch:
    `design-critique` and `accessibility-review` per page, `ux-copy` for
    labels, empty states and errors. Findings are fixed in this phase.
  - Consistent Streamlit theme (`.streamlit/config.toml`), typography,
    spacing and chart palette; one shared page header and footer with data
    freshness and attribution.
  - Navigation order that leads with what a reviewer cares about: Live →
    Trends → Notable → Definitions → Data health → About.
  - Empty, loading and error states; number and time formatting (local time
    with UTC on hover); mobile width; colour-blind-safe palette; alt text.
  - Before/after screenshots of each page kept for the README.
- **Notable edition tuning** (found reading the first published edition,
  9–15 Oct):
  - ISO timestamps leak into sentences ("(2026-10-15T21:37:46…)"): strip
    them before verification; the Pacific time still verifies.
  - Wrong Wikipedia articles for some designators (V10 Bronco → B-25
    Mitchell, C82T → Diamond DA40): fix the mapping in
    `dim_aircraft_type` and add a test against obvious mismatches.
  - Photo credits copied raw from Commons (one includes an email address
    and camera details): trim to the author's name and license.
  - Decide whether single-aircraft LLCs are shown as "private".
- **Repository cleanup** so it reads well to an interviewer:
  - Remove the LAN website: `edge/sky_scanner/web.py`,
    `aircraft_log.html`, the `web` Compose service, `WEB_PORT` /
    `EXPLORER_PASSWORD` settings if nothing else uses them, and
    `tests/test_web.py`. `aircraft_log.py` is still imported by
    `enrich_worker.py`, so keep only what the worker needs (or move it into
    the worker) once its type tables live in dbt seeds; trim
    `test_aircraft_log.py` to match.
  - Remove one-off and superseded files: `edge/sky_scanner/demo.py`,
    `edge/sql/explore.sql` (superseded by dbt and the MCP server). Fold
    anything still true from `edge/SPEC.md` and `edge/STATUS.md` into
    `edge/README.md`, then delete them; git history keeps the originals.
  - Format and lint consistently: `ruff format` and `ruff check` for
    Python, `sqlfluff` (BigQuery dialect, dbt templater) for models,
    `terraform fmt`; enforced in CI.
  - GitHub presentation: repository description, topics, website link,
    LICENSE, a social-preview image, and a README that opens with the live
    links, a screenshot and the architecture diagram.
  - Final sweep: no dead links, no TODOs pointing at removed files, and
    `.gitignore` covering every local-only file.

**Demo:**
- The Trends page on the live site, with one chart's numbers checked
  against a hand-written BigQuery query.
- Before/after screenshots of each page, desktop and mobile width.
- `git ls-files` before and after, with the full test suite and lint
  passing after the removals, and the edge stack still running without the
  `web` service.
- The GitHub repository page as a logged-out visitor sees it.

**Done when:** every page has passed the UI/UX review, the Trends page is
live, and the repository contains only files a reviewer should see.

### Phase 8: Hardening and launch
- Runbook; decision records 0001–0006; README with architecture, links and
  screenshots; `data-sources.md` with attribution; `access-tiers.md`.
- gitleaks over history → repository made public; CI workflows (plan, dbt
  CI, deploy).
- **Owner:** record a 2-minute demo video; review each component until you
  can explain it in an interview; **apply**.

**Demo:**
- **Recovery drill on the real system:** unplug the antenna and record the
  timeline: freshness alert email → reconnect → backfill check → data
  balances.
- A pull request showing the CI checks: terraform plan and the dbt CI
  build.
- A complete walkthrough of the public site, as a reviewer would see it.

**Done when:** the repository is public, the links in the README work, and
the application is submitted.

### Optional: band-allocation analysis
How should the receiver split listening time between 1090 MHz and 978
MHz? Uses `capture_intervals` and detections per band, with methods,
uncertainty and a recommendation. Only if time allows before applying;
otherwise it moves to §6.

## 6. After the deadline (before offers in December)
- Move enrichment workers into Cloud Run Jobs and retire Postgres at home,
  with a durable local outbox.
- Elementary anomaly report; a real Fivetran connector (Cloud Storage
  source) to compare with the replicator.
- The band-allocation analysis, if it did not make the deadline.
- More eval questions; publish a monthly eval trend.
