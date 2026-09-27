# Sky Scanner — status and live demonstration log

Last updated 2026-09-26 (UTC). Results below are separated into **demonstrated
with live RF**, **verified offline** (fixtures/mocks, never presented as live),
and **pending**. Live observations are public ADS-B broadcasts received by the
HackRF at the user's window; only small excerpts are reproduced here.

Hardware: HackRF One r9 (firmware v2.2.0) in USB SDR mode (1d50:6089), SRH789
antenna fully shortened, indoors at a window, near Las Vegas, NV.

## Demonstrated with live RF

### 1. 1090 MHz detection → ingestion → PostgreSQL (full Compose pipeline)

`docker compose up -d` started at 18:14:06 UTC. With the receiver, ingest,
enrichment and db services running unattended, the first two minutes produced:

| Capture interval (UTC) | Band | Duration | Decoded messages | Decoder exit |
| --- | --- | --- | --- | --- |
| 18:14:06.6–18:14:36.8 | 1090 MHz | 30.2 s | 85 | 0 |
| 18:14:37.3–18:15:12.3 | 978 MHz | 35.1 s | 0 | 1 (see below) |
| 18:15:12.8–18:15:43.0 | 1090 MHz | 30.1 s | 93 | 0 |
| 18:15:43.5–18:16:18.6 | 978 MHz | 35.1 s | 0 | 1 |

Stored: 8 new 15-second snapshots, 2 aircraft, 219 messages, all with
`frequency_hz = 1090000000`. Alternation is recorded in `capture_intervals`.

### 2. Enrichment of the new live snapshots (FAA registry, free)

From `enriched_snapshots` (as-of join):

| Bucket | ICAO | Callsign | Registration | Aircraft | Registered owner | Operator | Alt (ft) | GS (kt) | Position |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 18:14:15 | A5BD0B | SWA2813 | N469WN | BOEING 737-7H4 | SOUTHWEST AIRLINES CO | null | 35975 | 426 | 36.1644, −115.2497 |
| 18:16:30 | A5BD0B | SWA2813 | N469WN | BOEING 737-7H4 | SOUTHWEST AIRLINES CO | null | 36000 | 434 | 36.2902, −115.5399 |
| 18:14:15 | A6B81B | — | N532CP | HAWKER BEECHCRAFT CORP 4000 | VIKING HORIZON LLC | null | 24250 | 453 | not decoded |

Operator stays null: the registry owner is not treated as operator. A6B81B
remained queryable without a position.

### 3. Per-field timestamps and carry-forward

The `fields` JSONB keeps each value's original reception time, for example
`{"value": 36000, "source": "direct", "timestamp": 1790446604.202}`. The
18:15:00 bucket (a 1090 blind period) carried the 18:14:30 position with its
original timestamp rather than a new one.

### 4. Earlier direct runs (before the full pipeline)

- The HackRF decoded live 1090 MHz aircraft through the patched readsb.
- A direct recording run stored 69 messages as four snapshots for AD7ECE, which
  enrichment identified as N969NN, BOEING 737-823 (FL340, ~425 kt, near
  36.0856, −115.2249).

### 5. FAA automatic download

The enrichment container downloaded `ReleasableAircraft.zip` unattended (browser
User-Agent plus FAA referrer). It computed SHA-256
`7d65298830c17ed90b058da8bb3f230ef5eb603d3dbe45e3e07d466b3fddafd0`, matching the
already-imported dataset (316,992 records), and correctly skipped re-import. No
HEAD request is used.

### 6. Database access from the LAN address (tested from the host via 192.168.1.121)

- `explorer` with TLS: connected (TLSv1.3); writes rejected (InsufficientPrivilege).
- `explorer` without TLS: rejected by pg_hba.
- Wrong password: rejected.
- Owner account `sky_scanner` via LAN: rejected by pg_hba (owner only on the Docker network).
- The client source IP is preserved as 192.168.1.121, not a Docker gateway.
- **Bug found and fixed:** explorer lacked SELECT on tables created before the
  default-privilege rule. `sql/schema.sql` now grants explicitly on every start.

### 7. Container replacement and database-outage recovery (live pipeline)

- At about 18:20 UTC, the app containers were recreated with the rebuilt
  image, then the **database container itself** was force-recreated.
- The named volume `sky-scanner_postgres-data-v1` preserved all 36 snapshots
  and 316,992 metadata rows.
- During the database outage, ingest logged `database write deferred`, kept
  results in its bounded retry queue, then reconnected. Live snapshots resumed
  (42 rows, latest bucket 18:21:15).
- After the grant fix, explorer reads `enriched_snapshots` over TLS; its writes
  are still rejected.

### 8. OpenSky enrichment (crowdsourced, separate source)

- The enrichment worker found the newest snapshot
  (`aircraft-database-complete-2025-08.csv`) by S3 listing and imported
  587,401 records under `source_name = 'opensky'`.
- It identified 17 of the 18 observed aircraft with no FAA match:
  - long-haul airliners from Air France, KLM, Virgin Atlantic, Turkish,
    Air Tahiti Nui and Korean Air;
  - Air Canada, WestJet/Swoop and Porter jets;
  - AE6488/TALON95, listed as military serial 19-21054, type H60.
- ACF902, whose address decodes to N935HY, remains unidentified. It is absent
  from all FAA files and is possibly an FAA privacy (PIA) address.
- Some OpenSky data is stale. For example, 4CC54B lies in the Irish address
  block but is listed as TF-AME (Iceland). This is why OpenSky values appear
  only in `opensky_*` columns.

## 978 MHz (UAT): first live reception at 19:27 UTC

The unattended pipeline decoded **17 UAT messages** in the 978 MHz turn at
19:27:30–19:28:00 UTC. They were stored as 3 snapshots with
`frequency_hz = 978000000`:

| Bucket | ICAO | Callsign | Reg. | Aircraft | Alt baro/geom (ft) | GS (kt) | Track | Squawk |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 19:27:30 | AB86CF | — | N8416W | PIPER PA-28-180 | 4700 / 4825 | 77 | 128° | — |
| 19:28:00 | AB86CF | N8416W | N8416W | PIPER PA-28-180 | 4500 / 4575 | 81 | 129° | 1200 |

It was a VFR Cherokee (squawk 1200) descending southeast over northwest Las
Vegas, near 36.33, −115.32. The address qualifier was `adsb_icao` (direct), and
FAA enrichment matched it. This confirms the HackRF → SoapySDR → dump978-fa →
normalization → storage path for UAT with live RF.

By 21:22 UTC, 174 turns on 978 MHz had decoded 17 messages in total. UAT
traffic here is sparse, and FIS-B ground-station uplinks have still never been
decoded. Sensitivity on this band is therefore unknown and probably limited
by the antenna and placement.

### Earlier 978 diagnostics (before the first detection)

- dump978-fa opens the HackRF via SoapySDR (`driver=hackrf`) and streams
  unattended within the alternation. Zero messages decoded so far.
- Diagnostics (transient in-memory samples only; nothing written):
  - SoapySDR path at gains 20/40/64/90 dB, 25 s each: 0 uplink or downlink frames.
  - `hackrf_transfer` CS8 at 2.083 Msps → `dump978-fa --stdin`: samples flowed at
    the correct rate (~4.2 MiB/s, −20 dBfs average), 0 frames.
  - Offset-tuned 8.333 Msps capture (LO 976.5 MHz), shifted, low-pass filtered
    and decimated ×4 → dump978: full rate (16.8 MiB/s) for 30 s, 0 frames.
- Not even FIS-B ground-station uplinks were decoded. Aircraft UAT traffic is
  expected to be sparse. The antenna, placement, or local ground-station
  coverage may be the limit. UAT decoding correctness is verified only with
  fixtures.
- dump978-fa does not exit within the SIGTERM grace period (SoapySDR teardown
  stalls), so 978 turns end with SIGKILL, exit 1 and about 35 s duration. The
  interval now records `stop_signal_at` and the grace period is 3 s.

## Verified offline (fixtures/mocks — not live evidence)

- In the built image, 63 tests pass with none skipped. This includes a real
  patched-readsb binary fed fixture Mode S input (no RF).
- The metadata versioning SQL ran against a throwaway PostGIS container:
  - an unchanged refresh adds 0 rows;
  - changed owners get a new version;
  - removed aircraft get an `absent` version;
  - re-listed aircraft return to `present`.

## Pending validation

| Item | Needs |
| --- | --- |
| UAT sensitivity / uplink reception | An antenna trimmed for 978 MHz or outdoor placement (one live UAT aircraft decoded so far) |
| Host reboot → collection without login | A reboot (user-initiated) |
| USB unplug/replug recovery | Physical action; code backs off 5 s and retries |
| PortaPack returns to HackRF mode after power loss | Physical check |
| Sleep at the login screen after reboot | Session AC idle suspend is disabled (COSMIC `suspend_on_ac_time = None`); battery idle suspend stays at 2 h. The greeter's idle policy is unverified: after a reboot, leave the machine at the login screen for more than 30 min and confirm `capture_intervals` has no gap |
| LAN access from another machine | A second LAN client |
| Daily FAA refresh importing a *new* dataset | Wait for FAA's next publication |
| Pi receiver / remote DB | Deferred (needs a secured remote ingestion endpoint) |
