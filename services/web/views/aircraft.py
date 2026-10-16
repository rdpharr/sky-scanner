"""Aircraft log: every aircraft heard, with a detail view per aircraft."""

from __future__ import annotations

from datetime import timedelta

import altair as alt
import pandas as pd
import pydeck as pdk
import streamlit as st

from lib import bq
from lib.fmt import KIND_COLORS, local, local_naive, present

RANGES = {"24 hours": 1, "7 days": 7, "30 days": 30, "All time": None}

st.title("Aircraft log")


@st.cache_data(ttl=300, show_spinner="Loading aircraft…")
def aircraft() -> pd.DataFrame:
    return bq.run(f"""
        select address_type, address, registration, designator, type_name, aircraft_class,
               aircraft_kind, identity_source, owner_name, owner_category, operator, country,
               year_manufactured, is_military, military_rules, latest_callsign,
               first_seen_at, last_seen_at, snapshots, messages, is_confirmed, bands_mhz,
               max_altitude_baro_ft
        from {bq.table('pub_aircraft')}
    """)


df = aircraft()
c1, c2, c3, c4 = st.columns([1, 1, 1, 2])
period = c1.selectbox("Last heard", list(RANGES), index=1)
kinds = c2.multiselect("Kind", ["Airline", "Military", "Other"])
show_unconfirmed = c3.toggle("Show unconfirmed", help="Addresses heard in a single message, "
                             "never again. Usually decoding errors rather than aircraft.")
search = c4.text_input("Search", placeholder="Registration, callsign, type, operator or address")

view = df if show_unconfirmed else df[df.is_confirmed]
if RANGES[period]:
    view = view[view.last_seen_at >= pd.Timestamp.now(tz="UTC") - timedelta(days=RANGES[period])]
if kinds:
    view = view[view.aircraft_kind.isin(kinds)]
if search:
    text = view[["registration", "latest_callsign", "type_name", "operator", "address",
                 "designator"]].fillna("").astype(str).agg(" ".join, axis=1)
    view = view[text.str.contains(search, case=False, regex=False)]

m1, m2, m3, m4 = st.columns(4)
m1.metric("Aircraft", f"{len(view):,}")
m2.metric("Airline", f"{(view.aircraft_kind == 'Airline').sum():,}")
m3.metric("Military", f"{(view.aircraft_kind == 'Military').sum():,}")
m4.metric("Types", f"{view.designator.nunique():,}")

classes = view.groupby("aircraft_class").size().rename("aircraft").reset_index()
st.altair_chart(alt.Chart(classes).mark_bar().encode(
    x=alt.X("aircraft:Q", title="Aircraft"),
    y=alt.Y("aircraft_class:N", sort="-x", title=None)).properties(height=180), width="stretch")

table = view.sort_values("last_seen_at", ascending=False).assign(
    first_seen=lambda d: local(d.first_seen_at).dt.strftime("%Y-%m-%d %H:%M"),
    last_seen=lambda d: local(d.last_seen_at).dt.strftime("%Y-%m-%d %H:%M"))
selection = st.dataframe(
    table[["registration", "latest_callsign", "type_name", "aircraft_kind", "aircraft_class",
           "operator", "owner_name", "first_seen", "last_seen", "messages", "address"]],
    hide_index=True, width="stretch", height=420, on_select="rerun", selection_mode="single-row",
    column_config={"registration": "Registration", "latest_callsign": "Callsign",
                   "type_name": "Type", "aircraft_kind": "Kind", "operator": "Operator",
                   "messages": "Messages", "address": "ICAO address",
                   "aircraft_class": "Class", "owner_name": "Owner (organizations only)",
                   "first_seen": "First seen (PT)", "last_seen": "Last seen (PT)"})
st.caption("Select a row for its detail. Owner names are shown only for registered "
           "organizations; private owners are never named.")

rows = selection.selection.rows
if not rows:
    st.stop()
plane = table.iloc[rows[0]]

# ---------------------------------------------------------------- detail
st.divider()
title = next(v for v in (plane.registration, plane.latest_callsign, plane.address) if present(v))
st.header(f"{title} · {plane.type_name}")

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
    links = [f"[ADS-B Exchange](https://globe.adsbexchange.com/?icao={plane.address.lower()})",
             f"[FlightAware](https://flightaware.com/live/modes/{plane.address.lower()}/redirect)"]
    if isinstance(plane.registration, str) and plane.registration.startswith("N"):
        links.append(f"[FAA registry](https://registry.faa.gov/AircraftInquiry/Search/NNumberResult?nNumberTxt={plane.registration})")
    st.markdown(" · ".join(links))

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
