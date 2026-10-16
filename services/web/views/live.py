"""Live map: aircraft heard in the last few minutes, refreshed every 15 s."""

from __future__ import annotations

import time

import pandas as pd
import pydeck as pdk
import streamlit as st

from lib import bq
from lib.fmt import KIND_COLORS, local

REFRESH_SECONDS = 15
# Stop polling BigQuery when nobody has touched the page for a while, so a
# forgotten tab does not keep billing queries.
LIVE_MINUTES = 10
TRAIL_MINUTES = 10

st.title("Live")
st.caption("Aircraft heard by a receive-only ADS-B/UAT station near Las Vegas. "
           "Aircraft whose owners opted out of display (FAA LADD) are never shown.")

if "live_until" not in st.session_state or st.button("Resume live updates"):
    st.session_state.live_until = time.time() + LIVE_MINUTES * 60
polling = time.time() < st.session_state.live_until


def color(kind) -> list[int]:
    return KIND_COLORS.get(kind if isinstance(kind, str) else None, KIND_COLORS[None])


def load() -> pd.DataFrame:
    return bq.query_15s(f"""
        select bucket_start, address, band_mhz, callsign, latitude, longitude,
               altitude_baro_ft, ground_speed_kt, track_deg, squawk, message_count,
               registration, type_name, aircraft_class, aircraft_kind, operator,
               timestamp_diff(current_timestamp(), last_received_at, second) as age_seconds
        from {bq.table('pub_live_snapshots')}
        where bucket_start >= timestamp_sub(current_timestamp(), interval {TRAIL_MINUTES} minute)
    """)


def render() -> None:
    if polling and time.time() >= st.session_state.live_until:
        st.rerun()  # full rerun turns the timer off
    df = load()
    if df.empty:
        st.info(f"Nothing heard in the last {TRAIL_MINUTES} minutes. "
                "The receiver may be between turns or offline; see Data health.")
        return
    latest = df.sort_values("bucket_start").groupby("address").tail(1)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Aircraft in the last 2 min", int((latest.age_seconds <= 120).sum()))
    c2.metric(f"Aircraft in the last {TRAIL_MINUTES} min", len(latest))
    c3.metric("Messages", f"{int(df.message_count.sum()):,}")
    c4.metric("Newest report", f"{int(latest.age_seconds.min())} s ago")

    positioned = df.dropna(subset=["latitude", "longitude"]).sort_values("bucket_start")
    trails = pd.DataFrame([
        {"path": g[["longitude", "latitude"]].values.tolist(), "color": color(g.aircraft_kind.iloc[-1])}
        for _, g in positioned.groupby("address") if len(g) > 1
    ], columns=["path", "color"])
    points = positioned.groupby("address").tail(1).copy()
    points["color"] = points.aircraft_kind.map(color)
    points["label"] = (points.callsign.fillna(points.registration).fillna(points.address)
                       + " · " + points.type_name.fillna("type not yet identified"))
    points["alt"] = points.altitude_baro_ft.map(lambda a: f"{a:,.0f} ft" if pd.notna(a) else "altitude unknown")

    st.pydeck_chart(pdk.Deck(
        initial_view_state=pdk.ViewState(latitude=36.1, longitude=-115.2, zoom=7.2),
        layers=[
            pdk.Layer("PathLayer", trails, get_path="path", get_color="color",
                      width_min_pixels=2, opacity=0.5),
            pdk.Layer("ScatterplotLayer", points, get_position=["longitude", "latitude"],
                      get_fill_color="color", get_radius=1800, radius_min_pixels=4,
                      pickable=True),
        ],
        tooltip={"text": "{label}\n{alt}"},
    ), height=560)
    st.caption(f"Blue: airline · Red: military · Orange: other. Trails show the last {TRAIL_MINUTES} "
               f"minutes. {len(latest) - len(points)} aircraft were heard without a position.")

    table = latest.assign(heard=local(latest.bucket_start).dt.strftime("%H:%M:%S"))
    st.dataframe(
        table.sort_values("age_seconds")[["heard", "callsign", "registration", "type_name",
                                          "aircraft_kind", "operator", "altitude_baro_ft",
                                          "ground_speed_kt", "band_mhz", "address"]],
        hide_index=True, width="stretch",
        column_config={"heard": "Heard (PT)", "type_name": "Type", "aircraft_kind": "Kind",
                       "altitude_baro_ft": st.column_config.NumberColumn("Altitude (ft)", format="%d"),
                       "ground_speed_kt": st.column_config.NumberColumn("Speed (kt)", format="%d"),
                       "band_mhz": "MHz"})


if not polling:
    st.warning(f"Live updates paused after {LIVE_MINUTES} minutes without interaction.")
st.fragment(render, run_every=REFRESH_SECONDS if polling else None)()
