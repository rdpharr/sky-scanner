"""One aircraft: identity, photo of its type, every sighting and a sighting's track.

Opened from links elsewhere on the site: /aircraft-detail?address=ADFDF9 (and
&type=<address_type> for non-ICAO addresses). Not listed in the navigation.
"""

from __future__ import annotations

import altair as alt
import pandas as pd
import pydeck as pdk
import streamlit as st

from lib import bq
from lib.fmt import KIND_COLORS, local, local_naive, present

st.set_page_config(layout="wide")

address = (st.query_params.get("address") or "").upper()
address_type = st.query_params.get("type") or "icao"
st.page_link("views/aircraft.py", label="Aircraft log", icon=":material/arrow_back:")

found = bq.query_5m(f"""
    select * from {bq.table('pub_aircraft')}
    where address = @address and address_type = @address_type""", address=address, address_type=address_type)
if found.empty:
    st.title("Aircraft not found")
    st.info("Open an aircraft from the Aircraft log, Live or Notable pages. "
            + (f"No aircraft with address {address} has been heard." if address else ""), title="No aircraft chosen")
    st.stop()
plane = found.iloc[0].copy()
plane["first_seen"] = local(pd.Series([plane.first_seen_at])).iloc[0].strftime("%Y-%m-%d %H:%M")
plane["last_seen"] = local(pd.Series([plane.last_seen_at])).iloc[0].strftime("%Y-%m-%d %H:%M")

title = next(v for v in (plane.registration, plane.latest_callsign, plane.address) if present(v))
st.title(f"{title}, {plane.type_name}")

left, right = st.columns([2, 1])
with left:
    facts = {
        "ICAO address": f"{plane.address} ({plane.address_type})",
        "Registration": plane.registration,
        "Type": f"{plane.type_name} ({plane.designator})" if present(plane.designator) else plane.type_name,
        "Class / kind": f"{plane.aircraft_class} / {plane.aircraft_kind}",
        "Operator": plane.operator,
        "Owner": plane.owner_name if present(plane.owner_name) else plane.owner_category,
        "Country": plane.country,
        "Year built": int(plane.year_manufactured) if present(plane.year_manufactured) else None,
        "Identified by": plane.identity_source,
        "Bands": ", ".join(f"{int(b)} MHz" for b in plane.bands_mhz),
        "First / last seen (PT)": f"{plane.first_seen} / {plane.last_seen}",
    }
    st.table(pd.Series({k: v for k, v in facts.items() if present(v)}, name="").astype(str))
    if plane.is_military:
        st.markdown("**Military identification:** " + ", ".join(
            r.replace("_", " ") for r in plane.military_rules))
    links = st.container(horizontal=True)
    links.link_button("ADS-B Exchange", f"https://globe.adsbexchange.com/?icao={plane.address.lower()}",
                      icon=":material/open_in_new:")
    links.link_button("FlightAware", f"https://flightaware.com/live/modes/{plane.address.lower()}/redirect",
                      icon=":material/open_in_new:")
    if isinstance(plane.registration, str) and plane.registration.startswith("N"):
        links.link_button("FAA registry", "https://registry.faa.gov/AircraftInquiry/Search/NNumberResult"
                          f"?nNumberTxt={plane.registration}", icon=":material/open_in_new:")

with right:
    photo = bq.query_1h(f"""
        select image_url, wikipedia_article_url from {bq.table('pub_aircraft_types')}
        where designator = @designator""", designator=plane.designator if present(plane.designator) else "")
    if not photo.empty and pd.notna(photo.image_url.iloc[0]):
        st.image(photo.image_url.iloc[0], caption="A representative aircraft of this type, "
                 "not this airframe. Image: Wikimedia Commons.")
        st.markdown(f"[Wikipedia article]({photo.wikipedia_article_url.iloc[0]})")

sightings = bq.query_5m(f"""
    select started_at, ended_at, duration_minutes, snapshots, messages, bands_mhz, callsigns,
           squawks, min_altitude_baro_ft, max_altitude_baro_ft, max_ground_speed_kt,
           positioned_snapshots, path_nm, circling_ratio, emergency_squawk
    from {bq.table('pub_sightings')}
    where address_type = @address_type and address = @address
    order by started_at desc limit 500""", address_type=plane.address_type, address=plane.address)

st.subheader(f"Sightings ({len(sightings)})")
st.caption("A sighting is a run of reports with no gap longer than 10 minutes, across both bands.")
if sightings.empty:
    st.info("No sightings yet: the aircraft was heard after the last hourly build.")
    st.stop()
sightings = sightings.assign(start=local(sightings.started_at).dt.strftime("%Y-%m-%d %H:%M"))
picked = st.dataframe(
    sightings[["start", "duration_minutes", "messages", "callsigns", "min_altitude_baro_ft",
               "max_altitude_baro_ft", "max_ground_speed_kt", "positioned_snapshots", "path_nm"]],
    hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
    column_config={"start": "Start (PT)",
                   "duration_minutes": st.column_config.NumberColumn("Minutes", format="%.1f"),
                   "messages": "Messages", "callsigns": "Callsigns",
                   "min_altitude_baro_ft": st.column_config.NumberColumn("Min alt (ft)", format="%d"),
                   "max_altitude_baro_ft": st.column_config.NumberColumn("Max alt (ft)", format="%d"),
                   "max_ground_speed_kt": st.column_config.NumberColumn("Max speed (kt)", format="%d"),
                   "positioned_snapshots": "Positions",
                   "path_nm": st.column_config.NumberColumn("Path (nm)", format="%.1f")})
row = sightings.iloc[picked.selection.rows[0] if picked.selection.rows else 0]

track = bq.query_5m(f"""
    select bucket_start, latitude, longitude, altitude_baro_ft, ground_speed_kt, callsign, squawk
    from {bq.table('pub_snapshots')}
    where address_type = @address_type and address = @address
      and bucket_start between timestamp_sub(@start, interval 15 second) and @end
    order by bucket_start""", address_type=plane.address_type, address=plane.address,
    start=row.started_at.to_pydatetime(), end=row.ended_at.to_pydatetime())

st.subheader(f"Sighting starting {row.start} PT")
positions = track.dropna(subset=["latitude", "longitude"])
if len(positions):
    kind_color = KIND_COLORS.get(plane.aircraft_kind, KIND_COLORS[None])
    st.pydeck_chart(pdk.Deck(
        initial_view_state=pdk.ViewState(latitude=positions.latitude.mean(),
                                         longitude=positions.longitude.mean(), zoom=8),
        layers=[pdk.Layer("PathLayer", [{"path": positions[["longitude", "latitude"]].values.tolist()}],
                          get_path="path", get_color=kind_color, width_min_pixels=3),
                pdk.Layer("ScatterplotLayer", positions, get_position=["longitude", "latitude"],
                          get_fill_color=kind_color, radius_min_pixels=3)]), height=380)
else:
    st.info("No position was decoded during this sighting.")
profile = track.melt(id_vars="bucket_start", value_vars=["altitude_baro_ft", "ground_speed_kt"]).dropna()
if len(profile):
    profile["bucket_start"] = local_naive(profile.bucket_start)
    st.altair_chart(alt.Chart(profile).mark_line(point=True).encode(
        x=alt.X("bucket_start:T", title="Time (PT)"),
        y=alt.Y("value:Q", title=None),
        row=alt.Row("variable:N", title=None,
                    header=alt.Header(labelExpr="datum.value == 'altitude_baro_ft' ? 'Altitude (ft)' : 'Ground speed (kt)'"))
    ).properties(height=140).resolve_scale(y="independent"), width="stretch")
