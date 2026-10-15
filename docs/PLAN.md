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
                   replicator ── BigQuery Storage Write API (~30 s) ──▶  raw
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
                               (OpenRouter: MiMo-V2.6-Pro) → verifier → ai_notable_editions

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
│   └── STATUS.md, SPEC.md
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
│   │   ├── pages/               # live, aircraft log, notable, definitions, data health, about
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
    ├── deploy.yml               # on main: build images → Artifact Registry → terraform apply
    └── dbt-docs.yml             # on main: dbt docs generate → GitHub Pages
```

## 4. Components

### 4.1 Edge and replicator
- **`replicator.py`** (a new Compose service):
  - **Snapshots and capture intervals:** checks every 30 s from a saved
    high-water mark, re-sending a 2-minute overlap. It writes through the
    BigQuery Storage Write API; dbt staging removes duplicates on the
    primary key.
  - **Metadata tables:** changes since the last `id` are copied daily.
  - **Small reference tables** (military codes, callword lookups, type
    images, Doc 8643) are copied in full each day.
- **Credentials:** a service-account key that can write only to `raw`,
  stored in `edge/secrets/`. It is the only key in the system; ADR 0006
  explains why.
- **Backfill:** export all history from Postgres to Parquet, copy to Cloud
  Storage, then a BigQuery load job.
- **Enrichment change:** import the FAA `type_registrant` and `year_mfr`
  fields. These are filled in on existing rows without creating new registry
  versions. They drive owner categories and aircraft age.

### 4.2 BigQuery
| Dataset | Contents | Who can read it |
|---|---|---|
| `raw` | Replicated tables, unchanged | dbt only |
| `analytics` | Staging, intermediate, marts, ops, `ai_notable_editions`, eval results | dbt, notable job (writes its results), owner |
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
   to `analytics.ai_notable_editions` and exposed through a `public` view.
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

### 4.9 CI/CD and operations
- **CI** (`ci.yml`, `dbt-ci.yml`): tests, linting, gitleaks, plus a dbt
  build of only the changed models on every pull request.
- **Deploy:** on merge to main, build images then run `terraform apply`.
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

## 5. Schedule

| Date | Work | Who |
|---|---|---|
| **Thu Oct 15** | Install gcloud (done). Owner: billing account, `gcloud auth login` and `gcloud auth application-default login`. Move code into `edge/`. Prototype candidate scoring on Postgres. | both |
| **Fri Oct 16** | Terraform core: APIs, datasets, service accounts, registry, secrets, GitHub sign-in, budget, email channel. Replicator plus backfill. Owner: verify the domain in Google Search Console (TXT record in Cloudflare). | both |
| **Sat–Sun Oct 17–18** | dbt staging → marts, definitions, tests, public layer plus leak tests. `dbt_runner` job, schedules, email alerts. | Claude |
| **Mon Oct 19** | Streamlit Live / Log / Detail pages deployed; domain mappings. Owner: add DNS-only CNAMEs `skyscanner` and `mcp.skyscanner` → `ghs.googlehosted.com`. | both |
| **Tue Oct 20** | MCP server deployed and tested with MCP Inspector; dbt docs on Pages. | Claude |
| **Wed Oct 21** | Notable agent and verifier; first edition published. Owner: create the OpenRouter key with a credit limit and store it in Secret Manager. | both |
| **Thu Oct 22** | Q&A and edition evals, model comparison, Notable / Definitions / Data health pages. | Claude |
| **Fri Oct 23** | Runbook and recovery drill, decision records, README, gitleaks, repo goes public. Owner: record a 2-minute demo; review every component until it can be explained in an interview. | both |
| **Sat Oct 24** | Buffer, band-allocation analysis if time allows. **Owner applies.** | owner |

## 6. After the deadline (before offers in December)
- Move enrichment workers into Cloud Run Jobs and retire Postgres at home,
  with a durable local outbox.
- Elementary anomaly report; a real Fivetran connector (Cloud Storage
  source) to compare with the replicator.
- The band-allocation analysis, if it did not make the deadline.
- More eval questions; publish a monthly eval trend.
