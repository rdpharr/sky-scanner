# Sky Scanner — phase-one specification

Status: phase one implemented, 2026-09-26; see [STATUS.md](STATUS.md) for live results and pending validation. Confirmed requirements are distinguished from proposed implementation defaults below.

## Purpose and scope

Continuously operate a receive-only aircraft transponder collector on the user's computer in the USA. Store decoded observations in a database for SQL exploration from the local network. Use the attached HackRF, alternating 1090 MHz and 978 MHz, and record the actual receive frequency. Capture all detectable aircraft without a geographic filter.

Use Docker for the environment and a private repository in the user's personal GitHub account (proposed name: `sky-scanner`). No dashboard, raw I/Q recordings, raw message archive, paid enrichment, storage monitoring, alerts, automatic retention deletion, or public feeding service in phase one. Data is retained indefinitely and storage is supervised manually.

Snapshots deliberately discard intermediate changes within each 15-second interval; this is not a lossless transponder message archive. Continuous operation does not mean simultaneous or complete coverage of both bands.

## Reception and scheduling

- One process owns the HackRF and coordinates decoder selection and retuning. Never run two competing USB readers.
- Confirmed schedule: alternate 30-second turns on 1090 MHz and 978 MHz. Dwell durations are configurable independently of the 15-second snapshot intervals. Each band has roughly 30-second blind periods plus retuning overhead.
- Persist actual listening interval start/end times and frequency as observation provenance, including retuning gaps. This is collection metadata, not a monitoring subsystem.
- Decode Mode S/ADS-B aircraft data on 1090 MHz and UAT aircraft reports on 978 MHz. Preserve direct versus rebroadcast origin when available. Non-aircraft weather products are outside phase one.
- Frequency is receiver tuning frequency in integer Hz, not a guess about the original transmitter band for rebroadcast reports.
- Do not discard aircraft merely because position or identity enrichment is unavailable. Do not invent positions for Mode S reports without position data.
- Radio samples exist only transiently in memory or pipes. No transmission or RF recording.
- Validate each decoder with HackRF before fixing versions. Candidate 978 decoder: FlightAware dump978-fa via SoapySDR or a sample-stream adapter. A compatible sample rate and filtering/resampling path must be demonstrated; generic SoapySDR support alone is not proof of HackRF compatibility. Select a maintained 1090 decoder with a verified HackRF or sample-stream input.
- Retuning must discard incomplete sample buffers and preserve valid, timestamped decoding state where needed. Reject stale position pairs across time limits or incompatible sources.

## Snapshot semantics

- UTC-aligned, half-open 15-second buckets: [start, start + 15 seconds).
- Emit at most one snapshot per receiver, aircraft address identity, receive frequency, and bucket, only if at least one valid report was received for that identity on that frequency during the bucket.
- Combine latest available decoded fields. Each field retains its observation timestamp and source; carried values are never stamped as newly received. Proposed carry-forward expiry is 60 seconds, configurable; expired values become null. Identity enrichment is separate.
- Store first/last reception time and valid message count for the bucket. A single aircraft observed on two frequencies may have two rows with independent provenance.
- Scope identities by address type as well as address value. Do not assume anonymous UAT or non-ICAO addresses identify a registered aircraft. Do not infer cross-band identity matches from callsign alone.
- Use receiver reception time for bucketing, with a synchronized host clock. Store transmitted time separately if decoded and meaningful.
- Flush partial buckets on orderly shutdown. Uncommitted in-memory data may be lost on abrupt power failure; no zero-loss guarantee.

## Proposed architecture and data model

Docker Compose services: receiver/decoder supervisor, Python ingestion worker, PostgreSQL with PostGIS, and a separate enrichment worker. Enrichment failures must not stop collection. Receiver and database endpoints are configurable for eventual Pi/remote-database deployment. No message broker is required initially.

Tables:

| Table | Purpose |
| --- | --- |
| receivers | Stable receiver identifier and optional location/configuration metadata |
| capture_intervals | Actual band listening intervals and decoder version |
| aircraft_snapshots | 15-second observations, keyed for idempotent inserts |
| aircraft_metadata | Versioned external metadata with source and retrieval time |
| enrichment_imports | Source dataset version and import provenance |

Snapshot columns include bucket time, receiver ID, address value/type, frequency Hz, protocol/origin, first/last reception, message count, callsign, position, barometric/geometric altitude, speed, track/heading, vertical rate, squawk, on-ground/emergency indicators, and quality indicators wherever decoded. Store common values as typed columns with documented units; retain additional decoded fields and field timestamps in JSONB. Unknown fields remain null. Preserve altitude and velocity reference/type rather than combining unlike measurements.

Indexes support time ranges, aircraft history, and geographic queries. SQL views expose latest observations and enriched historical observations without silently rewriting historical metadata. Include example SQL for recent aircraft, an aircraft's path, aircraft without positions, and observations by frequency.

Inserts are transactional and retry-safe with a unique snapshot key. Proposed outage behavior: bounded memory retry buffer, no durable edge queue in phase one; longer database outages can lose observations. Durable forwarding can be added for a future remote deployment.

## Free enrichment

Start with the FAA releasable aircraft registry and aircraft reference data for registration, manufacturer/model, aircraft type, and registered owner. Operator is separate and nullable: registry owner is not automatically the operator. Add operator only where a free source provides an attributable match. Non-US, withheld, unmatched, and anonymous identities remain valid observations with missing enrichment.

Proposed refresh: initial bulk import and daily refresh when a newer source is available. Stage and validate imports before promoting them; keep the previous valid dataset if refresh fails. Record source URL, retrieval time, dataset identifier/hash, and effective time when supplied. Preserve metadata versions for historical exploration. Do not make one external request per aircraft snapshot.

## Deployment and access

- Enable Docker at boot and configure services to restart automatically without interactive login. Retry receiver discovery after USB disconnects and retry database connections with backoff.
- Store database data in a persistent volume; keep credentials and collected data out of Git.
- Expose PostgreSQL only on the intended LAN interface, with explicit client subnet rules, password authentication, and a read-only exploration role. Configure encrypted client access. No internet exposure or router port forwarding.
- Grant receiver USB access narrowly; do not require all containers to run privileged.
- Supply a manual backup/restore procedure. Automated backup scheduling is not part of phase one.
- Ordinary bounded service logs support troubleshooting; no metrics stack, storage watcher, alerts, or dashboard.
- Sleep, shutdown, USB absence, retuning, and database outages produce collection gaps. Boot startup does not prevent system sleep.
- Build for x86-64 first; keep dependencies compatible with ARM64 where practical. Actual Pi hardware validation is deferred.

## Acceptance criteria

1. Both frequency paths decode known valid test input; malformed input does not produce false aircraft records. Live verification is completed when hardware and signals are available.
2. The single receiver alternates bands, and every persisted snapshot carries the correct receive frequency.
3. Bucket boundaries, per-band identity separation, field ages, empty intervals, and retries obey the snapshot rules.
4. Aircraft with missing positions, anonymous addresses, or no registry match remain queryable.
5. Enrichment preserves provenance and distinguishes owner from operator; a failed refresh does not interrupt collection.
6. Data survives container replacement; a host reboot starts collection without login, and USB reconnection is recovered automatically.
7. An authorized LAN client can query using the exploration role; unauthorized connections are rejected.
8. No raw RF files, dashboard, monitoring service, or automatic observation deletion is introduced.

## Inspection and remaining decisions

Inspected 2026-09-26: Pop!_OS 24.04 LTS, x86-64, about 31 GiB RAM and 845 GiB free. Git 2.43.0 and HackRF tools 2023.01.1 are present. Docker, Docker Compose, and GitHub CLI are absent. hackrf_info found no HackRF. Detailed USB descriptors identify a connected PortaPack Mayhem at 1d50:6018 with a serial interface, despite the generic lsusb label of Black Magic Debug Probe. Switching to HackRF USB SDR mode is the next diagnostic step. Systemd reports failed fwupd-refresh and iw-set-regdomain services; no changes made. System package installation requires an interactive sudo password. No packages or GitHub repository have been created yet.

Follow-up USB verification succeeded after the user selected the correct mode: USB ID 1d50:6089, HackRF One board ID 4, hardware revision r9, firmware v2.2.0 (API 1.08). The installed hackrf_info utility successfully opens and identifies the receiver. RF reception and decoding have not yet been tested.

Before implementation: identify the antenna; validate decoder paths; authenticate GitHub; determine LAN bind address/client subnet. Before declaring unattended operation ready: confirm the desired sleep behavior and verify that HackRF mode is restored after power cycling. The carry-forward expiry, outage behavior, and enrichment refresh cadence above are proposed defaults rather than explicit user requirements.

## Implementation status (2026-09-26)

- Live 1090 MHz decoding, snapshot recording, and FAA enrichment were demonstrated through the full Compose pipeline.
- Band alternation is recorded in `capture_intervals`.
- 978 MHz runs unattended through dump978-fa/SoapySDR. The first live UAT aircraft (N8416W, 17 messages) was decoded and stored at 19:27 UTC. UAT traffic is sparse, and no ground-station uplinks have been decoded (see STATUS.md).
- Decoders: readsb 3b5368a with a per-message JSON patch (1090, 2.4 Msps); dump978-fa f40aa9a (978, ~2.083 Msps via SoapySDR). High-rate capture with resampling is not implemented. A diagnostic offset-tuned 8.333 Msps path with ×4 decimation did not decode UAT either.
- FAA metadata is versioned only on change, with an explicit `absent` status when an aircraft leaves the registry. This bounds growth from daily refreshes.
- Chosen defaults:
  - 60 s field carry-forward;
  - 5 s late-arrival grace (`INGEST_LATE_GRACE_SECONDS`);
  - bounded memory queues (10,000 receiver events, 20,000 snapshots);
  - daily FAA download, deduplicated by SHA-256.
- Not yet validated: host reboot, USB replug, PortaPack power-cycle mode, sleep behaviour at the login screen (session AC idle suspend is disabled), access from a second LAN machine, and a Pi/remote database.

## Technical references

- HackRF specifications: https://hackrf.readthedocs.io/en/stable/hackrf_one.html
- HackRF sampling guidance: https://hackrf.readthedocs.io/en/latest/sampling_rate.html
- FlightAware UAT decoder and interfaces: https://github.com/flightaware/dump978
- FAA registry downloads: https://www.faa.gov/licenses_certificates/aircraft_certification/aircraft_registry/releasable_aircraft_download
- FAA database field definitions: https://registry.faa.gov/database/ardata.pdf
