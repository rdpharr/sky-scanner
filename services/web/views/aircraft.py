"""Aircraft log: every aircraft heard, with a detail view per aircraft."""

from __future__ import annotations

from datetime import timedelta

import altair as alt
import pandas as pd
import streamlit as st

from lib import bq
from lib.fmt import SERIES, detail_url, local

RANGES = {"24 hours": 1, "7 days": 7, "30 days": 30, "All time": None}

st.set_page_config(layout="wide")  # maps, charts and tables use the full width

st.title("Aircraft log")


@st.cache_data(ttl=300, show_spinner="Loading aircraft…")
def aircraft() -> pd.DataFrame:
    return bq.run(f"""
        select address_type, address, registration, designator, type_name, aircraft_class,
               aircraft_kind, identity_source, owner_name, owner_category, operator, country,
               year_manufactured, is_military, military_rules, latest_callsign,
               first_seen_at, last_seen_at, snapshots, messages, is_confirmed, bands_mhz,
               max_altitude_baro_ft
        from {bq.table("pub_aircraft")}
    """)


df = aircraft()
c1, c2, c3, c4 = st.columns([1, 1, 1.2, 2], vertical_alignment="bottom")
period = c1.selectbox("Last heard", list(RANGES), index=1)
kinds = c2.multiselect("Kind", ["Airline", "Military", "Other"])
show_unconfirmed = c3.toggle(
    "Include unconfirmed",
    help="Addresses heard in a single message, never again. Usually decoding errors rather than aircraft.",
)
search = c4.text_input("Search", placeholder="Registration, callsign, type, operator or address")

view = df if show_unconfirmed else df[df.is_confirmed]
if RANGES[period]:
    view = view[view.last_seen_at >= pd.Timestamp.now(tz="UTC") - timedelta(days=RANGES[period])]
if kinds:
    view = view[view.aircraft_kind.isin(kinds)]
if search:
    text = (
        view[["registration", "latest_callsign", "type_name", "operator", "address", "designator"]]
        .fillna("")
        .astype(str)
        .agg(" ".join, axis=1)
    )
    view = view[text.str.contains(search, case=False, regex=False)]

m1, m2, m3, m4 = st.columns(4)
m1.metric("Aircraft", f"{len(view):,}")
m2.metric("Airline", f"{(view.aircraft_kind == 'Airline').sum():,}")
m3.metric("Military", f"{(view.aircraft_kind == 'Military').sum():,}")
m4.metric("Types", f"{view.designator.nunique():,}")

classes = view.groupby("aircraft_class").size().rename("aircraft").reset_index()
st.altair_chart(
    alt.Chart(classes)
    .mark_bar(
        color=SERIES["dark" if st.context.theme.type == "dark" else "light"][0],
        cornerRadiusEnd=4,
        height=alt.RelativeBandSize(0.7),
    )
    .encode(
        x=alt.X("aircraft:Q", title="Aircraft"),
        y=alt.Y("aircraft_class:N", sort="-x", title=None, axis=alt.Axis(labelLimit=220)),
        tooltip=[
            alt.Tooltip("aircraft_class:N", title="Class"),
            alt.Tooltip("aircraft:Q", title="Aircraft", format=","),
        ],
    )
    .properties(height=200),
    width="stretch",
    alt="Bar chart of aircraft in the current view by class",
)

table = view.sort_values("last_seen_at", ascending=False).assign(
    first_seen=lambda d: local(d.first_seen_at).dt.strftime("%Y-%m-%d %H:%M"),
    last_seen=lambda d: local(d.last_seen_at).dt.strftime("%Y-%m-%d %H:%M"),
)
text_columns = ["registration", "latest_callsign", "operator", "owner_name"]
shown = table.assign(**{c: table[c].astype("string").fillna("") for c in text_columns})  # blank, not "None"
shown["aircraft"] = [
    detail_url(a, t, r, c)
    for a, t, r, c in zip(table.address, table.address_type, table.registration, table.latest_callsign)
]
selection = st.dataframe(
    shown[
        [
            "aircraft",
            "latest_callsign",
            "type_name",
            "aircraft_kind",
            "aircraft_class",
            "operator",
            "owner_name",
            "first_seen",
            "last_seen",
            "messages",
            "address",
        ]
    ],
    hide_index=True,
    width="stretch",
    height=420,
    on_select="rerun",
    selection_mode="single-row",
    alt="Aircraft matching the filters, most recently heard first; select a row for its detail",
    column_config={
        "aircraft": st.column_config.LinkColumn("Aircraft", display_text=r"#(.+)$", help="Open the aircraft's page"),
        "latest_callsign": "Callsign",
        "type_name": "Type",
        "aircraft_kind": "Kind",
        "operator": "Operator",
        "messages": "Messages",
        "address": "ICAO address",
        "aircraft_class": "Class",
        "owner_name": "Owner (organizations only)",
        "first_seen": "First seen (PT)",
        "last_seen": "Last seen (PT)",
    },
)
st.caption(
    "Open an aircraft's page from its link, or select its row. Owner names are shown only for "
    "registered organizations; private owners are never named."
)

if selection.selection.rows:
    picked = table.iloc[selection.selection.rows[0]]
    st.switch_page("views/aircraft_detail.py", query_params={"address": picked.address, "type": picked.address_type})
