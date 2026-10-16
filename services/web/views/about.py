"""About the project."""

import streamlit as st

st.title("About Sky Scanner")
st.markdown("""
A HackRF software radio at a window near Las Vegas listens to aircraft
transponders on 1090 MHz (ADS-B, Mode S) and 978 MHz (UAT), decodes them, and
stores 15-second snapshots of every aircraft it hears. Identities come from the
FAA registry, tar1090-db, OpenSky and plane-alert-db.

**How it works**

- **At home:** the receiver, decoders and a PostgreSQL database run in Docker.
  A replicator streams new rows to BigQuery every 30 seconds.
- **In Google Cloud:** dbt builds tested, documented models in BigQuery every
  hour. Freshness checks run every 15 minutes, and failures send email alerts.
- **This site** (Streamlit on Cloud Run) reads only the `public` dataset. Private
  owners are never named, and aircraft on the FAA's LADD list or using privacy
  addresses are labeled. Tests check this on every build.

Source code, design decisions and the plan are on
[GitHub](https://github.com/rdpharr/sky-scanner).

**Data sources:** [FAA aircraft registry](https://www.faa.gov/licenses_certificates/aircraft_certification/aircraft_registry/releasable_aircraft_download) (public domain) ·
[tar1090-db](https://github.com/wiedehopf/tar1090-db) ·
[OpenSky aircraft database](https://opensky-network.org/data/aircraft) ·
[plane-alert-db](https://github.com/sdr-enthusiasts/plane-alert-db) (ODbL) ·
[VRS standing data](https://github.com/vradarserver/standing-data) (CC0) ·
[War Watch Intel](https://www.warwatchintel.com/codex) ·
ICAO Doc 8643 type designators via OpenSky ·
type photos from [Wikimedia Commons](https://commons.wikimedia.org).
""")
