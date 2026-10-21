# Runbook

How Sky Scanner runs, what each alert means and what to do about it, and how
to deploy, recover and check the system. Google Cloud project
`sky-scanner-pharr`, region `us-central1`, BigQuery location `US`.

Commands assume the repository root, `gcloud` signed in as the owner, and:

```sh
R=us-central1-docker.pkg.dev/sky-scanner-pharr/sky-scanner   # Artifact Registry
```

## What runs where

| Component | Where | Schedule | Runs as | Check it |
|---|---|---|---|---|
| Receiver, ingest, Postgres, enrichment | Home, Docker Compose (`edge/`) | Always on | Local roles | `docker compose ps` in `edge/` |
| Replicator (Postgres → BigQuery `raw`) | Home, Compose service `replicator` | Every 30 s | `sky-edge` (writes `raw` only) | `docker compose logs --since 10m replicator` |
| dbt build | Cloud Run job `dbt-hourly` | :05 past each hour (UTC) | `sky-dbt` | Data health page; `gcloud run jobs executions list --job dbt-hourly --region us-central1` |
| dbt build including daily-tagged models | Cloud Run job `dbt-daily` | 06:35 UTC | `sky-dbt` | as above |
| Source freshness | Cloud Run job `dbt-freshness` | Every 30 min | `sky-dbt` | as above |
| Website | Cloud Run service `web`, skyscanner.pharrhomelab.com | Always on | `sky-web` (reads `public`) | Open the site |
| MCP server | Cloud Run service `mcp`, mcp.skyscanner.pharrhomelab.com | Always on | `sky-mcp` (reads `public`) | `curl https://mcp.skyscanner.pharrhomelab.com/health` |
| Notable edition | Cloud Run job `notable-edition` | Mondays 06:00 Pacific | `sky-notable` (reads `public`, writes `ai`) | Notable page; `gcloud run jobs executions list --job notable-edition --region us-central1` |

Cloud Scheduler starts the jobs as `sky-scheduler`, which may only invoke them.
The project, its APIs, the budget and the Terraform state bucket come from
`infra/bootstrap.sh`; everything else in Google Cloud is in Terraform
(`infra/terraform/`), and the owner runs `terraform apply`. DNS records are
in Cloudflare. Terraform ignores Cloud Run image changes, so images are
rolled out with `gcloud` (see [Deploying](#deploying)).

## Alerts

All alerts email the operator (rdpharr@gmail.com; Gmail files them under
Updates) with a link to this runbook. The headings below are the alert
policies' names; each section gives the email's subject. dbt warnings are
never emailed: they appear on the Data health page and are expected from
time to time.

### Sky Scanner: data stopped arriving (critical)

Email subject: "Sky Scanner: no new `<table>` data". A source is past its
freshness limit.

| Source | Warning | Alert | Silence usually means |
|---|---|---|---|
| `raw.capture_intervals` | 5 min | 15 min | The receiver process, the edge database or the replicator is down |
| `raw.aircraft_snapshots` | 20 min | 60 min | The radio is listening but decoding nothing (antenna, PortaPack, HackRF) |
| `raw.aircraft_metadata` | 26 h | 50 h | The replicator's daily reload failed (freshness is measured on `_replicated_at`) |

1. At home, in `edge/`: `docker compose ps`. Every service should be up.
2. If `capture_intervals` is stale: `docker compose logs --since 30m receiver
   replicator`. If the replicator is failing, check the key
   (`secrets/gcp-edge.json`) and the network, then
   `docker compose up -d replicator`. It catches up from its watermark on
   its own; nothing is lost while Postgres keeps running.
3. If only `aircraft_snapshots` is stale, turns are being recorded but
   nothing is decoded: check the PortaPack is in **HackRF mode** (it can
   reset after power loss), the USB cable and the antenna, then
   `docker compose restart receiver`.
4. If only `aircraft_metadata` is stale, the replicator is streaming but its
   daily reload failed: look for the error in
   `docker compose logs --since 26h replicator`.
5. If the computer slept or rebooted, `docker compose up -d`.
6. Confirm recovery: the next `dbt-freshness` run (within 30 minutes) shows
   no errors on the Data health page, and the policy closes itself after
   30 minutes.

### Sky Scanner: data quality check failed

Email subject: "Sky Scanner: data check `<check>` `<status>`". A dbt test or
model failed in a scheduled build.

1. See the failure: Data health → Tests, or
   ```sh
   bq query --use_legacy_sql=false 'select finished_at, name, status, failures, message
     from `sky-scanner-pharr.analytics.dbt_node_results` where status in ("fail","error")
     order by finished_at desc limit 20'
   ```
2. Reproduce against dev from `dbt/`:
   `dbt build --profiles-dir . -s <model or test>+` (writes `dev_analytics`
   and `dev_public`).
3. Fix the model or the data, deploy the dbt runner, and run
   `gcloud run jobs execute dbt-hourly --region us-central1 --wait`.
4. A failure of `public_never_names_private_owners` means a private owner's
   name may be on the website and the MCP server. Treat it first: find the
   owner in the test's output and fix `macros/public_owner.sql` or the
   source data before anything else.

### Sky Scanner: scheduled job failed

Email subject: the policy name. A Cloud Run job execution failed: the dbt
runner could not run dbt at all, or the notable job errored or rejected its
edition. (A failing dbt test does not fail the job; it sends the data check
email instead.)

1. `gcloud run jobs executions list --job <job> --region us-central1 --limit 3`
2. Logs: `gcloud logging read 'resource.type="cloud_run_job" AND
   resource.labels.job_name="<job>"' --freshness 2h --limit 50`
3. Common causes:
   - **Image or start-up error** after a deploy: roll back with
     `gcloud run jobs update <job> --region us-central1 --image <previous tag>`.
   - **BigQuery quota or byte cap**: a query exceeded `maximum_bytes_billed`
     (2 GB per dbt query) or the daily per-user quota. Look for the model in
     the logs and make it cheaper (partition pruning, incremental).
   - **Notable edition rejected** (`edition_rejected` in the logs): fewer
     than 60% of statements verified or fewer than 3 entries. Nothing was
     published; last week's edition stays on the site. Look at the stored
     draft and verifier report in `ai.notable_editions`, then rerun (see
     [Notable editions](#notable-editions)).
   - **OpenRouter errors** (`401`, `402`, `429`): check the key and credit
     in OpenRouter, then rerun.

### Sky Scanner: freshness checks stopped running

Email subject: the policy name. No successful `dbt-freshness` run for an
hour, so the "data stopped arriving" alert cannot fire either.

1. `gcloud scheduler jobs describe dbt-freshness --location us-central1`:
   is it paused or failing to invoke?
2. Run it by hand: `gcloud run jobs execute dbt-freshness --region us-central1 --wait`.
3. If it fails, treat it as a scheduled job failure.

## Deploying

There is no deploy pipeline: CI (`.github/workflows/tests.yml`) runs lint
and tests on every push, and images are built and rolled out by hand.

```sh
gcloud auth configure-docker us-central1-docker.pkg.dev   # once
TAG=$(git rev-parse --short HEAD)

# Website and MCP server
docker build -t $R/web:$TAG services/web && docker push $R/web:$TAG
gcloud run services update web --region us-central1 --image $R/web:$TAG
docker build -t $R/mcp:$TAG services/mcp && docker push $R/mcp:$TAG
gcloud run services update mcp --region us-central1 --image $R/mcp:$TAG

# dbt runner: one image for all three jobs (built from the repository root)
docker build -f jobs/dbt_runner/Dockerfile -t $R/dbt-runner:$TAG . && docker push $R/dbt-runner:$TAG
for j in hourly daily freshness; do
  gcloud run jobs update dbt-$j --region us-central1 --image $R/dbt-runner:$TAG
done

# Notable job
docker build -t $R/notable:$TAG jobs/notable && docker push $R/notable:$TAG
gcloud run jobs update notable-edition --region us-central1 --image $R/notable:$TAG
```

After a deploy, update the matching `*_image` default in
`infra/terraform/variables.tf` so a rebuild from Terraform starts from the
same image. After a dbt change, run `dbt-hourly` once and check Data health.

Edge changes deploy at home: `docker compose build ingest && docker compose
up -d` in `edge/`. The receiver, ingest, enrichment and replicator services
share that image; the database runs the stock PostGIS image.

**Terraform:** `terraform plan` from `infra/terraform/`, review, then the
owner runs `terraform apply`. State is in `gs://sky-scanner-pharr-tfstate`.

## Routine operations

### Notable editions

The Monday job covers the seven Pacific days before the day it runs.
To write or rewrite an edition by hand (`--week-ending` is the day after the
last day covered):

```sh
gcloud run jobs execute notable-edition --region us-central1 --wait \
  --args=edition,--week-ending,2026-10-19
```

A rerun adds a new row to `ai.notable_editions`; the site shows the newest
published edition for each week. Use `--purpose test` to try a change
without publishing.

### Agent evals

From `jobs/notable/`, with the OpenRouter key read into the environment only:

```sh
export OPENROUTER_API_KEY=$(gcloud secrets versions access latest --secret openrouter-api-key)
python -m evals.run_evals qa --model xiaomi/mimo-v2.6-pro --model anthropic/claude-haiku-5.5
python run.py edition --week-ending 2026-10-03 --purpose eval   # the reference week
python -m evals.run_evals recall
```

Results go to `ai.eval_runs` and appear on the Agent evals page.

### Access tiers

`infra/check_access_tiers.sh` queries BigQuery as `sky-web`, `sky-mcp` and
`sky-notable` and fails if any can read beyond `public`. Run it after any
IAM or dataset change.

### Secrets

| Secret | Used by | Rotate |
|---|---|---|
| `openrouter-api-key` | `notable-edition` | Create a key in OpenRouter, then `printf %s "$KEY" \| gcloud secrets versions add openrouter-api-key --data-file=-`. The job reads `latest` on its next run. |
| `mcp-sql-token` | `mcp` (`run_sql` callers send it as `X-Sky-Scanner-Token`) | Generated by Terraform: `terraform apply -replace=random_password.mcp_sql_token`, then `gcloud run services update mcp --region us-central1 --update-labels=token-rotated=$(date +%s)` so a new revision reads the new version. |
| `edge/secrets/gcp-edge.json` | Replicator | Create a new key for `sky-edge`, replace the file, `docker compose up -d replicator`, then delete the old key. |
| `edge/secrets/server.crt`, `server.key` | Edge Postgres TLS | Expires a year after creation; renew and recreate the `db` container. |

Never print secret values or commit them; `edge/secrets/`, `.env` and
`terraform.tfvars` are ignored by Git.

### Column documentation

Public column descriptions are generated from `dbt/scripts/column_docs.py`.
After adding a public column, add its meaning there, then regenerate from
the warehouse's column list:

```sh
cd dbt
bq query --use_legacy_sql=false --format=csv --max_rows=5000 \
  'select table_name, column_name from `sky-scanner-pharr.public.INFORMATION_SCHEMA.COLUMNS`
   order by 1, ordinal_position' > /tmp/cols.csv
python scripts/column_docs.py /tmp/cols.csv
```

The `public_columns_are_documented` test warns about anything missing.

## Recovery

**Replicator outage.** Postgres keeps everything; on restart the replicator
streams from its last watermark with a short overlap and skips rows already
sent. Duplicates that slip through are removed in dbt staging. Verify by
comparing counts for the outage window in Postgres and in `raw`.

**A raw table was recreated or reloaded.** The scheduled jobs merge only the
last two days of snapshots, so rebuild the incremental snapshot fact and
everything after it in full, from `dbt/` as the owner (the Cloud Run jobs
take no extra arguments):

```sh
dbt build --profiles-dir . --target prod --full-refresh -s fct_snapshots+
```

**Edge database lost.** Restore the latest `pg_dump` backup as described in
[edge/README.md](../edge/README.md#backup-and-restore). BigQuery `raw` keeps
everything replicated before the loss, so the public site keeps its
history; the replicator resumes from the restored data.

**A Cloud Run revision is bad.** Roll back to the previous image with
`gcloud run services update <service> --region us-central1 --image <previous tag>`
(`gcloud run revisions list --service <service> --region us-central1` lists them).

**Custom domain or certificate.** The `skyscanner` and `mcp.skyscanner`
CNAMEs in Cloudflare must stay **DNS only** (grey cloud) so Google can issue
and renew the managed certificates. A new mapping can take up to an hour to
get its certificate.

## Cost guardrails

- Billing budget of $25 per month with emails at 50%, 90% and 100% (set by
  `infra/bootstrap.sh`).
- BigQuery query quota of 100 GiB per user per day (Terraform).
- Byte caps per query: dbt 2 GB, website 500 MB, MCP `run_sql` 1 GB (dry run
  first).
- Cloud Run is the main cost, and it bills an instance at the full rate for
  as long as any request is open. Open connections, not traffic, drive it:
  - The website keeps a websocket per open tab. Tabs idle for 15 minutes go
    to a static "paused" page (`services/web/app.py`), and the request
    timeout is 5 minutes. The one warm instance (`min_instance_count = 1`)
    costs about $13 a month when idle.
  - The MCP server refuses the optional `GET /mcp` stream (405), and its
    request timeout is 60 seconds. Before that, connected Claude sessions
    held it busy all day (about $1.30 a day).
  - `dbt-freshness` runs every 30 minutes, mostly dbt start-up time.
- Billing data is exported to the `billing` dataset (standard usage cost;
  enabled in the console under Billing → Billing export → BigQuery export).
  Cost by service for the last 7 days:
  ```sh
  bq query --use_legacy_sql=false 'select service.description service, sku.description sku,
      round(sum(cost),2) cost, round(sum((select sum(amount) from unnest(credits))),2) credits
    from `sky-scanner-pharr.billing.gcp_billing_export_v1_*`
    where usage_start_time > timestamp_sub(current_timestamp(), interval 7 day)
    group by 1,2 having cost > 0.01 order by cost desc'
  ```
- Measured when scheduling started (2026-10-16): about 610 GB of queries a
  month, within the 1 TB free tier; most of it is the 10 MB minimum per
  test. It grows with history, since several models rebuild in full. Notable editions cost about
  $0.25 each in OpenRouter credit.

## Drill log

**Alert drill, 2026-10-16 (UTC).** The replicator was stopped while
receiving continued, to prove the freshness alert end to end.

| Time | Event |
|---|---|
| 02:24:03 | Newest capture interval reached BigQuery |
| 02:24:28 | Replicator stopped (receiver and Postgres kept running) |
| 02:31:46 | Freshness: `capture_intervals` 8 min old: warning, no email (as designed) |
| 02:46:23 | Freshness: 22 min old: error logged |
| 02:46:59 | Alert email received, 36 s after the error |
| 02:49:12 | Replicator restarted |
| ~02:51 | Caught up: all 46 snapshots from the outage in BigQuery, matching Postgres |
| 02:54:24 | Freshness: 0 errors, 0 warnings |

**Database container replaced under load, 2026-09-26.** The edge `db`
container was force-recreated while collecting. The volume kept every row;
ingest held results in its retry queue and resumed writing.

**First scheduled-style notable run, 2026-10-16.** `notable-edition` ran on
Cloud Run for 9 to 15 October: 93 of 125 statements verified, 18 entries,
published, $0.25.
