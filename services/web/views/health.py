"""Data health: every data check, scheduled runs and receiver uptime."""

import altair as alt
import pandas as pd
import streamlit as st

from lib import bq
from lib.fmt import ago, local, local_naive

st.title("Data health")
st.markdown(
    "Every hour dbt rebuilds the data and runs its tests; every 15 minutes it checks "
    "that new data is still arriving. Failures email the operator. Warnings are "
    "shown here and are expected from time to time: they flag data worth a look, "
    "such as altitudes no aircraft could fly.")

checks = bq.query_5m(f"""
    select check_type, check_name, latest_status, latest_failures, latest_age_seconds,
           last_checked_at, last_passed_at, runs_7d, failed_runs_7d, warned_runs_7d
    from {bq.table('pub_data_health')}""")

if checks.empty:
    st.info("No scheduled checks have run yet.")
    st.stop()

status = checks.latest_status.replace({"success": "pass"})
c1, c2, c3, c4 = st.columns(4)
c1.metric("Checks", len(checks))
c2.metric("Passing", int((status == "pass").sum()))
c3.metric("Warnings", int((status == "warn").sum()))
c4.metric("Failing", int(status.isin(["fail", "error", "runtime error"]).sum()))

st.subheader("Is data arriving?")
fresh = checks[checks.check_type == "source"].copy()
fresh["newest data"] = fresh.latest_age_seconds.map(ago)
st.dataframe(fresh[["check_name", "latest_status", "newest data"]], hide_index=True,
             width="stretch", column_config={"check_name": "Source", "latest_status": "Status"})

st.subheader("Tests")
tests = checks[checks.check_type == "test"].assign(
    last_checked=lambda d: local(d.last_checked_at).dt.strftime("%Y-%m-%d %H:%M"))
order = {"fail": 0, "error": 0, "warn": 1, "pass": 2}
tests = tests.sort_values(by="latest_status", key=lambda s: s.map(order).fillna(3))
st.dataframe(tests[["check_name", "latest_status", "latest_failures", "failed_runs_7d",
                    "warned_runs_7d", "last_checked"]],
             hide_index=True, width="stretch", height=360,
             column_config={"check_name": "Test", "latest_status": "Status",
                            "latest_failures": "Failing rows", "failed_runs_7d": "Failed (7 d)",
                            "warned_runs_7d": "Warned (7 d)", "last_checked": "Last run (PT)"})

st.subheader("Scheduled runs")
runs = bq.query_5m(f"""
    select mode, started_at, duration_seconds, nodes, errors, warnings, bytes_billed, passed
    from {bq.table('pub_dbt_runs')}
    where started_at >= timestamp_sub(current_timestamp(), interval 2 day)
    order by started_at desc""")
runs = runs.assign(started=local(runs.started_at).dt.strftime("%m-%d %H:%M"),
                   mb_billed=(runs.bytes_billed / 1e6).round(0))
st.dataframe(runs[["started", "mode", "passed", "errors", "warnings", "nodes",
                   "duration_seconds", "mb_billed"]], hide_index=True, width="stretch",
             height=260, column_config={"started": "Started (PT)", "mb_billed": "MB billed"})

st.subheader("Receiver listening time")
uptime = bq.query_5m(f"""
    select hour_start, listening_seconds_1090 / 3600 as mhz_1090, listening_seconds_978 / 3600 as mhz_978
    from {bq.table('pub_receiver_uptime_hourly')}
    where hour_start >= timestamp_sub(current_timestamp(), interval 7 day)""")
if len(uptime):
    long = uptime.melt(id_vars="hour_start", var_name="band", value_name="share")
    long["hour_start"] = local_naive(long.hour_start)
    long["band"] = long.band.map({"mhz_1090": "1090 MHz", "mhz_978": "978 MHz"})
    st.altair_chart(alt.Chart(long).mark_area().encode(
        x=alt.X("hour_start:T", title="Hour (PT)"),
        y=alt.Y("share:Q", stack=True, title="Share of the hour listening",
                axis=alt.Axis(format="%"), scale=alt.Scale(domain=[0, 1])),
        color=alt.Color("band:N", title=None)).properties(height=220), width="stretch")
    st.caption("Gaps are outages; the small shortfall from 100% is retuning between bands.")
