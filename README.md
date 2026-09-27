# Sky Scanner

A receive-only HackRF collector that alternates 1090 MHz and 978 MHz every 30
seconds, stores 15-second decoded aircraft snapshots in PostgreSQL/PostGIS, and
enriches them from the free FAA registry. The first release is for SQL exploration.

## Run

On a Linux host with Docker Engine and Compose, put the PortaPack in **HackRF
mode** and connect an antenna. Only one process may use the radio at a time.

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

FAA metadata is versioned on change: a daily refresh writes rows only for new
or changed aircraft, plus a `registry_status = 'absent'` version when an
aircraft leaves the registry. Unchanged refreshes add no metadata rows.
The worker also imports the newest monthly [OpenSky aircraft database](https://opensky-network.org/data/aircraft)
snapshot. It adds international and military addresses the FAA registry lacks.
OpenSky data is crowdsourced, unlicensed and provided "as is", and is sometimes
stale. It is stored as a separate source (`source_name = 'opensky'`) and exposed
only in `opensky_*` view columns (registration, model, type code, operator,
owner, country). It never replaces FAA values. A snapshot URL that was already
imported is skipped without downloading. Set `OPENSKY_ENABLED=0` to disable it.
`enriched_snapshots` joins metadata available as of each observation;
`enriched_snapshots_current` explicitly joins the latest imported metadata.
`latest_aircraft` contains the most recent observation per identity/band, even
if it is old: filter by `last_received_at` to find recently heard aircraft.
More examples are in [sql/explore.sql](sql/explore.sql).

Each `fields` JSONB entry contains `value`, its original reception `timestamp`
(Unix seconds), and `source`. A snapshot can carry fields for up to 60 seconds;
their timestamps never advance unless received again. Null typed columns mean
the value was unknown or expired. Different frequency observations remain
separate. Rebroadcast reception frequency is not the original transmitter band.

## Live demos and tests

```sh
# This demo queries genuinely collected records; it does not inject fixtures.
docker compose exec ingest python -m sky_scanner.demo

# Offline/mocked regression suite; actual decoder fixture tests run in the image.
docker compose run --rm --no-deps ingest python -m unittest discover -s tests -v

# Explicit free-registry refresh (regular worker already runs daily).
docker compose run --rm --no-deps enrichment python -m sky_scanner.enrich_worker --once
```

For a terminal-only live decoder demo, stop the receiver service first, then
run `docker compose run --rm --no-deps receiver python -m sky_scanner.receiver
--stdout --seconds 120`. Restart `docker compose up -d receiver` afterwards.
Do not run two radio consumers together.

## Operational limits

- Band switching has deliberate blind periods and decoder startup overhead.
- 978 MHz UAT reception is live-verified but sparse: one aircraft in the first
  3 hours, and no ground-station uplinks decoded. See [STATUS.md](STATUS.md).
  dump978-fa stalls on SIGTERM, so 978 turns end with SIGKILL after 3 s.
- The native decoder paths use 2.4 Msps (1090) and approximately 2.083333 Msps
  (978). These are below HackRF's preferred 8 Msps minimum for filtering quality;
  improved high-rate capture/resampling is a future RF optimization, not a claim
  of current maximum sensitivity.
- No raw I/Q or raw transponder archive is retained. Snapshots discard
  intermediate changes inside each bucket.
- Collection uses bounded memory queues. Long database/network outages or
  power loss can lose observations. Events arriving more than
  `INGEST_LATE_GRACE_SECONDS` (default 5) after their bucket ends are dropped
  with a service log. Batches rejected by ingestion (HTTP 4xx) are dropped, not retried.
- The FAA registry does not establish actual operator identity, flight route,
  or withheld registration details. Operator remains null without another source.
- Ordinary bounded container logs support troubleshooting. There is no storage
  monitoring, alert system, automated pruning, or dashboard.
- The receiver container has USB bus access for reconnects, without privileged
  mode. Database and application containers have no USB access.

## Backup and restore

```sh
mkdir -p data
docker compose exec -T db pg_dump -U sky_scanner -d sky_scanner -Fc > data/backup.dump
```

Keep backups separately from this computer. For a restore test, create a separate
empty database, then use `pg_restore --no-owner -U sky_scanner -d RESTORE_DATABASE`.
Never restore over the live database without an explicit recovery plan. Backups
and collected data are ignored by Git. No automatic backup job is installed.

## Development and migration

Application code is Python; decoder revisions are pinned in the Dockerfile.
The readsb patch emits message-local decoded fields as JSON rather than polling
cached aircraft state. `decoder/patch_readsb.py` refuses changed source anchors.
Upstream projects: [readsb](https://github.com/wiedehopf/readsb) (GPL-3.0) and
[dump978](https://github.com/flightaware/dump978) (BSD-2-Clause). Their notices stay
in the build sources; retain corresponding source and patch if distributing binaries.

For a future Pi, build/validate ARM64 decoder images, run the receiver on the
Pi, and configure `INGEST_URL` to a secured remote ingestion endpoint. The current
HTTP ingestion endpoint is intended only for the private Docker network; do not
publish it directly to a LAN or internet. The DB location uses `DATABASE_URL`.

The approved functional scope is in [SPEC.md](SPEC.md); current live-demo
results and pending validation are in [STATUS.md](STATUS.md).
