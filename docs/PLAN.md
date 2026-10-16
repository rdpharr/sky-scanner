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
| Public data | FAA, OpenSky, tar1090-db, plane-alert-db, VRS and War Watch Intel, all with attribution. Excludes LADD/PIA aircraft, individual owners' names and the receiver location. |
| Infrastructure | Terraform from day 1; one service account per component; GitHub signs in to Google Cloud without stored keys (Workload Identity Federation) |
| Cloud region | us-central1 (supports Cloud Run custom domains) |
| CI/CD | GitHub Actions for tests and deployment only; scheduled work runs in Google Cloud |

### Open items
- Confirm that LADD/PIA aircraft and individual owners' names stay out of public views. This is currently assumed.
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
| **About 10% of the week's aircraft are LADD/PIA** (232 of 2,363). The K-MAX was LADD-flagged. | The public layer removes them before scoring, so the agent never sees them. |
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
  GitHub sign-in, Artifact Registry, OpenRouter secret, email channel.
  The per-account query quota is pending one more `terraform apply`.
- Notable-scoring prototype on Postgres (findings in §4.8).

**Demo (done):** the prototype's real results. Rare types surfaced the
VC-25A 92-9000, an F/A-18F and a K-MAX (LADD-flagged, so excluded from
public). Raw circling turned out to be dominated by flight schools.

### Phase 1: Data lands in BigQuery
- `replicator.py` and its Compose service (snapshots, capture intervals and
  receivers every ~30 s; metadata and reference tables daily).
- Key for `sky-edge` created locally into `edge/secrets/`, never in
  Terraform state.
- Full history backfilled from Postgres (Parquet → BigQuery load job).
- FAA `type_registrant` and `year_mfr` added to the edge importer.

**Demo:**
- Postgres vs BigQuery row counts per table, side by side.
- Age of the newest BigQuery row in seconds, run twice a minute apart to
  show live arrival.
- Real queries: aircraft heard in the last 10 minutes; this week's VC-25A
  sighting; busiest hours by band.
- Bytes billed for each query, to show what queries cost.

**Done when:** counts match, and live rows arrive within a minute.

### Phase 2: dbt models and canonical definitions
- dbt project: staging (removing duplicates), the registry history in
  `int_metadata_versions`, identity precedence, military classification,
  sightings and path features, marts, the `public` views, metric
  definitions and seeds (type tables ported from `aircraft_log.py`).
- Tests: keys, ranges, relationships, overlapping intervals, leak tests,
  Postgres parity.

**Demo:**
- `dbt build` output, every model and test.
- The definitions with real numbers: confirmed vs unconfirmed aircraft,
  sightings per aircraft, identification rate by source, military
  aircraft by the rule that fired.
- The same weekly numbers from the marts and from the old LAN page, to
  show they agree.
- A leak test passing on `public`, then deliberately pointed at
  `analytics` to show it failing.
- dbt docs lineage, viewed locally.

**Done when:** `dbt build` passes and the parity checks agree.

### Phase 3: Scheduling, data quality and alerting
- `dbt_runner` Cloud Run Job: hourly build, freshness checks every 15
  minutes. It loads run results into `analytics` ops tables.
- Log-based alert policies → email. Job-failure alerts.

**Demo:**
- Scheduled executions listed in Cloud Run.
- `ops_dbt_runs` and `ops_test_results` queried live.
- **A real alert:** stop the replicator until snapshot freshness errors.
  The email arrives in your inbox. Restart it and the next run passes.
  Record the timeline.

**Done when:** an alert email has arrived and recovery was confirmed.

### Phase 4: Website (Streamlit)
- Live, Aircraft log and Detail pages on Cloud Run; caching; Definitions
  and Data health pages.
- Domain mapping for `skyscanner.pharrhomelab.com`.
- **Owner:** verify `pharrhomelab.com` in Google Search Console (TXT
  record in Cloudflare); add the DNS-only CNAME `skyscanner` →
  `ghs.googlehosted.com`.

**Demo:**
- The live URL, with the map updating as aircraft move.
- A real aircraft's detail page checked against the same query run by
  hand.
- The web service account trying to read `analytics` and being refused.
- Cold-start time and bytes billed per page load.

**Done when:** the site is up on its own domain over HTTPS.

### Phase 5: MCP server
- Tools from §4.7 on Cloud Run at `mcp.skyscanner.pharrhomelab.com/mcp`,
  plus stdio mode; dbt docs published to GitHub Pages.
- **Owner:** DNS-only CNAME `mcp.skyscanner` → `ghs.googlehosted.com`.

**Demo:**
- MCP Inspector listing the tools and calling a few.
- Claude Code connected to the live endpoint, answering real questions
  ("what military aircraft were heard this week?", "what counts as a
  sighting?").
- Each answer checked against SQL.
- `run_sql` refusing a query on `analytics` and one over the byte cap.

**Done when:** an agent connected over the public URL answers correctly.

### Phase 6: Notable aircraft agent and evals
- Agent loop (OpenRouter, MiMo-V2.6-Pro) over the public MCP endpoint;
  verifier; `ai.notable_editions`; weekly schedule; Notable page.
- Q&A eval (~25 questions, fixed "golden week") and edition eval; model
  comparison; results page.
- **Owner:** create a dedicated OpenRouter key with a credit limit and add
  it with `gcloud secrets versions add openrouter-api-key --data-file=-`.

**Demo:**
- An edition generated on demand.
- Its full tool-call trace walked through.
- The verifier's report: which claims passed, which were removed and why.
- The published page.
- Eval scores per model, with token cost.

**Done when:** the first edition is live and the eval results are
published.

### Phase 7: Website polish and repository cleanup
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
- **UI/UX pass** across every page using Anthropic's `frontend-design`
  skill:
  - Consistent Streamlit theme (`.streamlit/config.toml`), typography,
    spacing and chart palette; one shared page header and footer with data
    freshness and attribution.
  - Navigation order that leads with what a reviewer cares about: Live →
    Trends → Notable → Definitions → Data health → About.
  - Empty, loading and error states; number and time formatting (local time
    with UTC on hover); mobile width; colour-blind-safe palette; alt text.
  - Before/after screenshots of each page kept for the README.
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
