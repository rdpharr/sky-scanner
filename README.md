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

## Community aircraft database (tar1090-db)

Every 30 minutes the enrichment worker looks up newly observed aircraft in
[tar1090-db](https://github.com/wiedehopf/tar1090-db), the community database
(derived from Mictronics) behind readsb/tar1090. It is downloaded daily. Only
aircraft actually observed are stored, as the `tar1090_db` source (partial,
change-only imports), with its flags (military, PIA, LADD) as tags. Rows
marked "miscode" identify transponders broadcasting invalid addresses. The
repository states no license: the data stays in this private database and is
never committed. Identity precedence is FAA, then tar1090-db, then OpenSky,
then the plane-alert-db military list. Set `TAR1090_DB_ENABLED=0` to disable
it.

The aircraft log hides "unconfirmed" addresses by default. These were heard in
only one message and never again, and are usually decoding errors rather than
real aircraft. Choose "Show unconfirmed" to include them.

## Military aircraft identification

The enrichment worker identifies military aircraft and decodes only those.
Detection runs locally against every aircraft, with no per-aircraft network
calls. An aircraft counts as military when any of these holds:

- its ICAO address is in the US military block (AE0000–AFFFFF);
- tar1090-db flags it as military;
- it is on the [plane-alert-db](https://github.com/sdr-enthusiasts/plane-alert-db)
  military list (ODbL, refreshed daily), which gives operator, type, serial
  and tags;
- its callsign starts with a military ICAO operator code (for example RCH →
  Air Mobility Command), from [VRS standing data](https://github.com/vradarserver/standing-data)
  (CC0, refreshed daily);
- it uses a US Coast Guard style callsign (C + tail number) from a
  US-military address.

For military aircraft only, tactical callwords (TALON95 → TALON) are looked
up in the [War Watch Intel](https://www.warwatchintel.com/codex) callsign
codex. Lookups run every 15 minutes, at most 10 per cycle, 5 s apart, and
are cached: 30 days when found, 7 days when not found. The result lists which
aircraft types and countries recently used the callword, and whether this
aircraft's type is among them. Callwords are shared across units, so this is
supporting evidence, not a unit identification.

Results appear in the aircraft log's Military filter and in a "Military
identification" panel on each military aircraft's detail view. Set
`MILITARY_ENABLED=0` to disable it. Reference data lives in the
`plane_alert_mil` metadata source, `military_operator_codes` and
`callword_lookups`.

## Aircraft type photos

Every 30 minutes the enrichment worker resolves up to 15 aircraft types seen
in the last 30 days to an English Wikipedia article. It uses Wikipedia's
"List of aircraft type designators" first, then a search on the model name,
accepted only when the article describes an aircraft. Results are cached in
`aircraft_type_images` (30 days when found, 7 days when not). Only freely
licensed Wikimedia Commons images are used. The detail page hotlinks the
image rather than storing a copy. Set `TYPE_IMAGES_ENABLED=0` to disable it.

## Aircraft log page (LAN)

The `web` service serves a read-only page at `http://<LAN IP>:8080` (port
`WEB_PORT`, bound to `DB_BIND_ADDRESS`). It shows:

- totals by aircraft class and ICAO type;
- a sortable, filterable list of every aircraft, with first and last seen
  dates in the browser's time zone;
- a **Refresh** button that queries the database live;
- a time-range filter (24 h, 7 days, 30 days, all time).

Select any aircraft in the list to open its detail view
(`#/aircraft/<type>/<address>`, which can be bookmarked). It shows:

- FAA and OpenSky identity side by side, plus links to ADS-B Exchange,
  FlightAware, OpenSky and the FAA record;
- first and last seen, and every past sighting (a run of snapshots with no
  gap over 10 minutes);
- for a selected sighting, a track plot, an altitude/ground-speed profile and
  every 15-second snapshot with all its decoded fields;
- the latest decoded fields with their reception times;
- the full registry version history;
- a representative photo of the aircraft's type from its English Wikipedia
  article, hotlinked from Wikimedia, with links to the article and the
  photo's credit and license page. It shows the type, not that airframe.

The page uses the read-only `explorer` role over TLS. The JSON endpoints are
`/api/aircraft?days=N`, `/api/aircraft/<type>/<address>` and
`/api/aircraft/<type>/<address>/snapshots?start=<iso>&end=<iso>` (at most
two days per request). It has no login and uses plain HTTP, so keep
it on the home network and never port-forward it. The first load fetches the
ICAO Doc 8643 type list from OpenSky; without it, classes show as "Other".
Aggregating all snapshots slows as data grows; use a shorter range if an
all-time refresh becomes slow.

Existing installs need the explorer rule for the Docker network in
`pg_hba.conf` (fresh installs get it from `docker/init-db.sh`):
`hostssl sky_scanner explorer 172.16.0.0/12 scram-sha-256`, followed by
`SELECT pg_reload_conf();`.

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
  monitoring, alert system, or automated pruning. The only UI is the read-only
  LAN aircraft log page.
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
