"""About the project."""

import streamlit as st

st.set_page_config(layout="centered")  # mostly prose: keep lines readable

st.title("About Sky Scanner")
st.markdown("""
A software radio in a window in northwest Las Vegas listens to aircraft
transponders on 1090 MHz (ADS-B and Mode S) and 978 MHz (UAT). Every aircraft it
hears is recorded in 15-second snapshots, identified from public registries, and
published here within minutes.
""")

st.header("How it works")
st.markdown("""
- **At home**, a HackRF receiver, its decoders and a PostgreSQL database run in
  Docker. A replicator streams new rows to BigQuery every 30 seconds.
- **In BigQuery**, dbt rebuilds tested, documented models every hour and checks
  every 15 minutes that data is still arriving; failures email the operator.
  Every number on this site comes from one definition in dbt.
- **For AI agents**, an [MCP server](https://mcp.skyscanner.pharrhomelab.com/mcp)
  serves the same data and definitions as tools, so an agent and a person asking
  the same question get the same answer.
- **Every Monday**, an AI agent writes the Notable page through that server. A
  program checks each sentence against the data the agent cited and removes any
  that do not match; Agent evals measures how well it does.
""")
st.page_link("views/definitions.py", label="Definitions", icon=":material/menu_book:")
st.page_link("views/health.py", label="Data health", icon=":material/monitor_heart:")

st.header("Privacy")
st.markdown("""
The site, the MCP server and the agent can read only a public layer of the data.
Owners who are private individuals are never named. Aircraft whose owners asked
the FAA to limit tracking (the LADD list) or that use privacy addresses (PIA) are
shown, because they are received directly over the air, and are labeled. Tests
check these rules on every build.
""")

st.header("Data sources")
st.markdown("""
- [FAA aircraft registry](https://www.faa.gov/licenses_certificates/aircraft_certification/aircraft_registry/releasable_aircraft_download) (public domain)
- [tar1090-db](https://github.com/wiedehopf/tar1090-db)
- [OpenSky aircraft database](https://opensky-network.org/data/aircraft)
- [plane-alert-db](https://github.com/sdr-enthusiasts/plane-alert-db) (ODbL)
- [VRS standing data](https://github.com/vradarserver/standing-data) (CC0)
- [War Watch Intel](https://www.warwatchintel.com/codex)
- ICAO Doc 8643 type designators, via OpenSky
- Type photos from [Wikimedia Commons](https://commons.wikimedia.org), credited under each photo
""")

st.markdown("Source code, design decisions and the build plan are on "
            "[GitHub](https://github.com/rdpharr/sky-scanner).")
