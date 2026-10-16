# Sky Scanner edge

The part of Sky Scanner that runs at home: a receive-only HackRF collector
that alternates between 1090 MHz and 978 MHz, stores 15-second decoded
aircraft snapshots in PostgreSQL/PostGIS, enriches them from free registries,
and replicates them to BigQuery. Everything downstream (dbt, the website, the
MCP server and the weekly agent) is described in the [top-level README](../README.md).

Hardware: a HackRF One r9 (firmware v2.2.0) in USB SDR mode, in a PortaPack,
with an SRH789 antenna fully shortened, indoors at a window in northwest Las
Vegas.

## Run

On a Linux host with Docker Engine and Compose, put the PortaPack in **HackRF
mode** and connect an antenna. Only one process may use the radio at a time.
Run every command in this document from the `edge/` directory.

```sh
python3 scripts/configure.py --bind-address YOUR_LAN_IP --lan-subnet YOUR_LAN_CIDR
docker compose build ingest
docker compose up -d
```

The setup script creates private, ignored `.env` credentials and a self-signed
TLS certificate in `secrets/`. It will not overwrite existing credentials.
Pin the host LAN address with a DHCP reservation so database clients and the
configured port binding continue to work after reboot.

Docker must be enabled at boot (`sudo systemctl enable --now docker`). Compose
services use `restart: unless-stopped`. Stop them with `docker compose stop`;
resume with `docker compose up -d`. A manually stopped service stays stopped
after reboot. Sleep and shutdown interrupt reception. The PortaPack must also
return to HackRF mode after power loss; verify this on your particular device.

Services: `receiver` (owns the radio and the decoders), `ingest` (builds
snapshots and writes them), `db`, `enrichment` (registries, military
identification, type photos) and `replicator` (BigQuery). Enrichment and
replication failures never stop collection.

## Reception and snapshots

- **Schedule.** One process owns the HackRF and alternates turns: 50 s on
  1090 MHz and 10 s on 978 MHz (`DWELL_1090_SECONDS`, `DWELL_978_SECONDS`).
  It was 30 s each until 2026-09-29; 978 traffic here is rare. Each turn is
  recorded in `capture_intervals`, so every gap (the other band, retuning,
  decoder teardown) is known. 1090 has blind periods of about 14 s, 978 about
  50 s.
- **Decoders.** readsb (pinned, patched to emit each message's decoded fields
  as JSON) on 1090 MHz at 2.4 Msps, and dump978-fa through SoapySDR on 978 MHz
  at about 2.083 Msps. `decoder/patch_readsb.py` refuses changed source
  anchors. Upstream: [readsb](https://github.com/wiedehopf/readsb) (GPL-3.0)
  and [dump978](https://github.com/flightaware/dump978) (BSD-2-Clause); their
  notices stay in the build sources.
- **Snapshots.** UTC-aligned, half-open 15-second buckets. At most one row per
  receiver, address type and address, frequency and bucket, written only when
  a valid report arrived for it. Typed columns hold common values with their
  units; the `fields` JSONB keeps every decoded field with its original
  reception `timestamp` and `source`. Values carry forward for up to 60 s and
  their timestamps never advance unless received again. The frequency is the
  receiver's tuning frequency, not a guess at the transmitter's band.
- **Identity.** Identities are scoped by address type as well as address;
  anonymous UAT and non-ICAO addresses are never assumed to be registered
  aircraft, and no cross-band match is inferred from a callsign alone.
  Aircraft without a position or a registry match remain queryable.
- **What is not kept.** No raw I/Q or raw message archive. Snapshots discard
  intermediate changes inside a bucket.

## Explore the database

Connect a PostgreSQL client using the LAN IP, port **5432**, database
**sky_scanner**, user **explorer**, and `EXPLORER_PASSWORD` from `.env`.
Use TLS `verify-full` and trust `secrets/server.crt`. That certificate is public
and may be copied to clients; never share `server.key` or the admin password.
The certificate expires after one year. Renew it and recreate the DB container
before expiry; the persistent database volume is unaffected.

The exploration role has SELECT access only. Access is restricted to the
configured LAN subnet. No decoder ports or ingestion endpoints are published.

```sql
SELECT bucket_start, address, frequency_hz, callsign,
       altitude_baro_ft, latitude, longitude
FROM aircraft_snapshots
ORDER BY bucket_start DESC LIMIT 20;

SELECT address, registration, manufacturer, model,
       altitude_baro_ft, metadata_retrieved_at
FROM enriched_snapshots_current
ORDER BY bucket_start DESC LIMIT 20;
```

`enriched_snapshots` joins the metadata available as of each observation;
`enriched_snapshots_current` joins the latest imported metadata.
`latest_aircraft` holds the most recent observation per identity and band,
even if it is old: filter by `last_received_at` for recently heard aircraft.

## Enrichment

**FAA registry.** Downloaded daily and deduplicated by SHA-256. Metadata is
versioned on change: a refresh writes rows only for new or changed aircraft,
plus a `registry_status = 'absent'` version when an aircraft leaves the
registry. The registered owner is never treated as the operator.

**OpenSky.** The newest monthly [OpenSky aircraft database](https://opensky-network.org/data/aircraft)
snapshot adds international and military addresses the FAA registry lacks.
It is crowdsourced, provided "as is" and sometimes stale, so it is stored as a
separate source (`source_name = 'opensky'`) and never replaces FAA values.
A snapshot already imported is skipped. `OPENSKY_ENABLED=0` disables it.

**tar1090-db.** Every 30 minutes, newly observed aircraft are looked up in
[tar1090-db](https://github.com/wiedehopf/tar1090-db), the community database
behind readsb/tar1090, downloaded daily. Only aircraft actually observed are
stored, as the `tar1090_db` source, with its flags (military, PIA, LADD) as
tags; "miscode" rows mark transponders broadcasting invalid addresses.
Identity precedence is FAA, then tar1090-db, then OpenSky, then
plane-alert-db. `TAR1090_DB_ENABLED=0` disables it.

**Military identification.** Detection runs locally against every aircraft,
with no per-aircraft network calls. An aircraft counts as military when any
of these holds:

- its ICAO address is in the US military block (AE0000–AFFFFF);
- tar1090-db flags it as military;
- it is on the [plane-alert-db](https://github.com/sdr-enthusiasts/plane-alert-db)
  military list (ODbL, refreshed daily);
- its callsign starts with a military ICAO operator code (for example RCH,
  Air Mobility Command), from [VRS standing data](https://github.com/vradarserver/standing-data)
  (CC0, refreshed daily);
- it uses a US Coast Guard style callsign from a US military address.

For military aircraft only, tactical callwords (TALON95 → TALON) are looked
up in the [War Watch Intel](https://www.warwatchintel.com/codex) callsign
codex: every 15 minutes, at most 10 per cycle, 5 s apart, cached 30 days when
found and 7 when not. Callwords are shared across units, so a match is
supporting evidence, not a unit identification. `MILITARY_ENABLED=0`
disables it.

**Aircraft type photos.** Every 30 minutes the worker resolves up to 15
aircraft types seen in the last 30 days (military first) to an English
Wikipedia article: through Wikipedia's "List of aircraft type designators"
first, otherwise by searching the model name. A search hit is kept only when
the article describes an aircraft and its title names the same model.
Results are cached in `aircraft_type_images` (30 days when found, 7 when
not). Only freely licensed Wikimedia Commons images are used, and they are
hotlinked, not stored. `TYPE_IMAGES_ENABLED=0` disables it.

## Replication to BigQuery

The `replicator` service copies the edge database into the BigQuery `raw`
dataset with the `sky-edge` service account, whose key is mounted from
`secrets/gcp-edge.json` and which can write nothing else. It connects as the
read-only `explorer` role, so replication never writes to the edge database.

- Snapshots and capture intervals are polled every 30 s
  (`REPLICATOR_INTERVAL_SECONDS`) and streamed in order of `written_at`, with
  a short overlap so rows from late commits are not missed. Rows already sent
  are skipped; duplicates that remain (for example after a restart) are
  removed in dbt staging.
- Reference tables are reloaded in full once a day with free load jobs.
- The receiver's `location` never leaves home.

## Tests

```sh
# Offline regression suite (fixtures and mocks; decoder fixture tests run in the image).
docker compose run --rm --no-deps ingest python -m unittest discover -s tests -v

# Explicit registry refresh (the worker already runs daily).
docker compose run --rm --no-deps enrichment python -m sky_scanner.enrich_worker --once
```

For a terminal-only live decoder check, stop the receiver service first, then
run `docker compose run --rm --no-deps receiver python -m sky_scanner.receiver
--stdout --seconds 120`. Restart it with `docker compose up -d receiver`
afterwards. Never run two radio consumers together.

## What has been verified live

- 1090 MHz: the full pipeline (decoder, ingest, Postgres, FAA enrichment)
  from the first day, 2026-09-26, with per-field timestamps and
  carry-forward checked in stored rows.
- 978 MHz: the first UAT aircraft (a Piper PA-28 squawking VFR over northwest
  Las Vegas) on 2026-09-26. UAT traffic here is sparse and no FIS-B
  ground-station uplink has ever been decoded, so sensitivity on this band is
  unknown and probably limited by the antenna and placement. Diagnostics at
  several gains, through `hackrf_transfer`, and through an offset-tuned
  8.333 Msps path also decoded no uplinks.
- Recovery: the database container was force-recreated under live load; the
  volume kept every row, ingest held results in its retry queue and resumed.
- Access: `explorer` over TLS can read but not write; plain connections, wrong
  passwords and the owner account from the LAN are all rejected.

Still to verify: collection after a host reboot without login, USB
unplug and replug, the PortaPack returning to HackRF mode after power loss,
and sleep behaviour at the login screen.

## Operational limits

- Band switching has deliberate blind periods and decoder startup overhead.
  dump978-fa stalls on SIGTERM, so 978 turns end with SIGKILL after 3 s.
- Both decoders run below HackRF's preferred 8 Msps minimum for filtering
  quality; high-rate capture with resampling is a possible RF improvement,
  not a claim of current maximum sensitivity.
- Collection uses bounded memory queues. Long database outages or power loss
  can lose observations. Events arriving more than `INGEST_LATE_GRACE_SECONDS`
  (default 5) after their bucket ends are dropped with a log line; batches
  rejected by ingestion (HTTP 4xx) are dropped, not retried.
- The FAA registry does not establish the operator, route or withheld
  registration details.
- The receiver container has USB bus access for reconnects, without
  privileged mode. The database and application containers have no USB access.
- Data freshness and pipeline failures are monitored in Google Cloud, which
  emails the operator; the edge itself has no storage monitoring or
  automated pruning.

## Backup and restore

```sh
mkdir -p data
docker compose exec -T db pg_dump -U sky_scanner -d sky_scanner -Fc > data/backup.dump
```

Keep backups separately from this computer. For a restore test, create a
separate empty database, then use `pg_restore --no-owner -U sky_scanner -d
RESTORE_DATABASE`. Never restore over the live database without an explicit
recovery plan. Backups and collected data are ignored by Git. No automatic
backup job is installed.

## Moving the receiver

For a future Raspberry Pi, build and validate ARM64 decoder images, run the
receiver on the Pi, and point `INGEST_URL` at a secured remote ingestion
endpoint. The current HTTP ingestion endpoint is meant only for the private
Docker network; never publish it to a LAN or the internet. The database
location is set by `DATABASE_URL`.

## References

- [HackRF One](https://hackrf.readthedocs.io/en/stable/hackrf_one.html) and
  [sampling guidance](https://hackrf.readthedocs.io/en/latest/sampling_rate.html)
- [FAA registry downloads](https://www.faa.gov/licenses_certificates/aircraft_certification/aircraft_registry/releasable_aircraft_download)
  and [field definitions](https://registry.faa.gov/database/ardata.pdf)
