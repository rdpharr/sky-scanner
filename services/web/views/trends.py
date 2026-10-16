"""Trends: daily and hourly traffic since collection began, normalized by
how long the receiver was actually listening."""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from lib import bq
from lib.fmt import KIND_HEX, MUTED, SERIES

LOW_UPTIME = 0.8  # below this share of the day or hour listening, counts understate traffic
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
dark = st.context.theme.type == "dark"
series = SERIES["dark" if dark else "light"]
SURFACE = "#141B24" if dark else "#FAFBF8"  # Streamlit's background: the gap between adjacent marks

st.set_page_config(layout="wide")  # maps, charts and tables use the full width

st.title("Trends")


@st.cache_data(ttl=3600, show_spinner="Loading trends…")
def load() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    # Complete Pacific days only: today is still being counted.
    today = "current_date('America/Los_Angeles')"
    mix = bq.run(f"""select day, aircraft_class, aircraft_kind, identity_source, confirmed_aircraft, sightings
                     from {bq.table('pub_daily_mix')} where day < {today}""")
    hourly = bq.run(f"""select hour_start, day_pt, hour_pt, weekday_pt, listening_seconds, uptime_ratio,
                               confirmed_aircraft, aircraft_per_listening_hour
                        from {bq.table('pub_hourly_traffic')} where day_pt < {today}""")
    bands = bq.run(f"""select day, band_mhz, confirmed_aircraft, listening_seconds
                       from {bq.table('pub_daily_traffic')} where day < {today}""")
    return mix, hourly, bands


def definition(label: str, **target: str) -> None:
    """Link to the Definitions page, opened at a metric (metric=) or a public view (model=)."""
    st.page_link("views/definitions.py", label=label, icon=":material/menu_book:", query_params=target)


def table_view(df: pd.DataFrame, alt_text: str, **column_config) -> None:
    with st.expander("Data table", icon=":material/table:"):
        st.dataframe(df, hide_index=True, width="stretch", alt=alt_text, column_config=column_config)


mix, hourly, bands = load()
if mix.empty:
    st.info("Trends appear after the first complete day of data.", title="No complete days yet")
    st.stop()

for df, col in ((mix, "day"), (hourly, "day_pt"), (bands, "day")):
    df[col] = pd.to_datetime(df[col])

# A day's uptime: listening time over the hours in that Pacific day (23 or 25 on DST changes).
uptime = hourly.groupby("day_pt").agg(listening=("listening_seconds", "sum"), hours=("hour_start", "size"))
uptime = (uptime.listening / (uptime.hours * 3600)).rename("uptime").reset_index().rename(columns={"day_pt": "day"})
daily = (mix.groupby("day").agg(aircraft=("confirmed_aircraft", "sum"), sightings=("sightings", "sum"))
         .join(mix[mix.aircraft_kind == "Military"].groupby("day").confirmed_aircraft.sum().rename("military"))
         .fillna({"military": 0}).reset_index().merge(uptime, on="day", how="left"))
first, last = daily.day.min(), daily.day.max()
low_days = daily[daily.uptime < LOW_UPTIME]

st.markdown(
    f"Collection began on {first:%-d %B %Y}, so these charts cover **{len(daily)} complete days** "
    f"({first:%-d %b} to {last:%-d %b}): enough to see daily and weekly rhythms, not long-run trends. "
    "Counts are confirmed aircraft (at least two messages), in Pacific time.")

# Headline: the last complete 7 days against the 7 before, only when both are complete.
st.subheader(f"Last 7 days ({last - pd.Timedelta(days=6):%-d %b} to {last:%-d %b})")
recent = daily[daily.day > last - pd.Timedelta(days=7)]
previous = daily[(daily.day <= last - pd.Timedelta(days=7)) & (daily.day > last - pd.Timedelta(days=14))]
has_previous = len(previous) == 7


daily["window"] = pd.cut((last - daily.day).dt.days, [-1, 6, 13, 10_000],
                         labels=["Last 7 days", "Previous 7 days", "Earlier"]).astype(str)


def sparkline(col: str, label: str, value_format: str) -> alt.Chart:
    """Daily bars under a tile. The last 7 days carry the accent colour; the
    tooltip gives the day's value against the whole period's average, the
    receiver's uptime that day and the comparison window."""
    data = daily.assign(value=daily[col], vs_average=daily[col] / daily[col].mean() - 1)
    return alt.Chart(data).mark_bar(cornerRadiusEnd=2).encode(
        x=alt.X("yearmonthdate(day):O", axis=None, scale=alt.Scale(paddingInner=0.25)),
        y=alt.Y("value:Q", axis=None, scale=alt.Scale(zero=True)),
        color=alt.condition(alt.datum.window == "Last 7 days", alt.value(series[0]), alt.value(MUTED)),
        opacity=alt.condition(alt.datum.window == "Earlier", alt.value(0.45), alt.value(1.0)),
        tooltip=[alt.Tooltip("day:T", title="Day", format="%A %-d %B"),
                 alt.Tooltip("value:Q", title=label, format=value_format),
                 alt.Tooltip("vs_average:Q", title=f"Vs {len(daily)}-day average", format="+.0%")]
        + ([] if col == "uptime" else [alt.Tooltip("uptime:Q", title="Receiver uptime", format=".0%")])
        + [alt.Tooltip("window:N", title="Window")],
    ).properties(height=64)


def tile(column, label: str, col: str, fmt: str, value_format: str, help: str) -> None:
    value = recent[col].mean()
    before = previous[col].mean() if has_previous else None
    delta = None if before in (None, 0) or pd.isna(before) else f"{(value - before) / before:+.0%}"
    with column.container(border=True):
        st.metric(label, fmt.format(value), delta=delta, delta_color="off" if col == "uptime" else "normal",
                  delta_description="vs previous 7 days" if delta else None, help=help)
        st.altair_chart(sparkline(col, label.removesuffix(" per day"), value_format), width="stretch",
                        alt=f"Daily {label.lower()} since collection began; the last 7 days are highlighted")


row = st.columns(4)
for column, (label, col, fmt, value_format, help_text) in zip(row, (
        ("Aircraft per day", "aircraft", "{:,.0f}", ",.0f", "Average distinct confirmed aircraft heard per day."),
        ("Military aircraft per day", "military", "{:,.1f}", ",.0f",
         "Average distinct confirmed military aircraft per day."),
        ("Sightings per day", "sightings", "{:,.0f}", ",.0f",
         "Average sightings per day: an aircraft heard again after 10 minutes of silence starts a new one."),
        ("Receiver uptime", "uptime", "{:.0%}", ".0%",
         "Share of the time the receiver was listening on either band."))):
    tile(column, label, col, fmt, value_format, help_text)
st.caption("Bars show each day since collection began; the last 7 days are highlighted. "
           "Hover over a bar for the day's value, its receiver uptime and how it compares.")
if not has_previous:
    st.caption("Week-over-week changes appear once there are 14 complete days.")


def low_uptime_layer(days: pd.DataFrame) -> alt.Chart:
    """Grey bands behind days the receiver listened less than 80% of the time."""
    spans = days.assign(start=days.day - pd.Timedelta(hours=12), end=days.day + pd.Timedelta(hours=12))
    return alt.Chart(spans).mark_rect(color=MUTED, opacity=0.15).encode(
        x="start:T", x2="end:T",
        tooltip=[alt.Tooltip("day:T", title="Day", format="%a %-d %b"),
                 alt.Tooltip("uptime:Q", title="Receiver uptime", format=".0%")])


# Daily aircraft by kind.
st.header("Aircraft heard each day", divider="gray")
kinds = ["Airline", "Other", "Military"]
by_kind = (mix.groupby(["day", "aircraft_kind"], as_index=False).confirmed_aircraft.sum()
           .merge(daily[["day", "aircraft", "uptime"]], on="day"))
by_kind["share"] = by_kind.confirmed_aircraft / by_kind.aircraft
bars = alt.Chart(by_kind).mark_bar(width=alt.RelativeBandSize(0.8), stroke=SURFACE, strokeWidth=1).encode(
    x=alt.X("yearmonthdate(day):T", title=None, axis=alt.Axis(format="%-d %b", grid=False)),
    y=alt.Y("confirmed_aircraft:Q", title="Confirmed aircraft", stack=True),
    color=alt.Color("aircraft_kind:N", title=None, sort=kinds,
                    scale=alt.Scale(domain=kinds, range=[KIND_HEX[k] for k in kinds]),
                    legend=alt.Legend(orient="top")),
    order=alt.Order("kind_order:Q"),
    tooltip=[alt.Tooltip("day:T", title="Day", format="%A %-d %B"), alt.Tooltip("aircraft_kind:N", title="Kind"),
             alt.Tooltip("confirmed_aircraft:Q", title="Aircraft of this kind", format=","),
             alt.Tooltip("share:Q", title="Share of the day", format=".0%"),
             alt.Tooltip("aircraft:Q", title="All aircraft that day", format=","),
             alt.Tooltip("uptime:Q", title="Receiver uptime", format=".0%")],
).transform_calculate(kind_order=f"indexof({kinds}, datum.aircraft_kind)")
st.altair_chart((low_uptime_layer(low_days) + bars).properties(height=280), width="stretch",
                alt="Stacked daily bars of confirmed aircraft by kind: airline, other and military")
if len(low_days):
    st.caption(f"Grey bands mark days the receiver listened less than {LOW_UPTIME:.0%} of the time "
               "(outages or a partial first day); their counts understate traffic.")
table_view(by_kind.pivot(index="day", columns="aircraft_kind", values="confirmed_aircraft").fillna(0)
           .astype(int).reset_index().merge(uptime, on="day", how="left"),
           "Confirmed aircraft per day by kind, with receiver uptime",
           day=st.column_config.DateColumn("Day", format="ddd D MMM"),
           uptime=st.column_config.NumberColumn("Receiver uptime", format="percent"))
definition("Definition: confirmed aircraft heard", metric="confirmed_aircraft_heard")

# Normalized by listening time: only well-covered hours, so outages don't read as quiet days.
st.header("Aircraft per listening hour", divider="gray")
st.markdown("Raw daily counts fall when the receiver is down. Dividing each hour's aircraft by the time "
            f"spent listening, and using only hours with at least {LOW_UPTIME:.0%} uptime, gives a rate "
            "that compares fairly across days.")
good = hourly[hourly.uptime_ratio >= LOW_UPTIME]
rate = (good.groupby("day_pt", as_index=False)
        .agg(rate=("aircraft_per_listening_hour", "mean"), hours=("hour_start", "size"),
             busiest=("aircraft_per_listening_hour", "max"))
        .rename(columns={"day_pt": "day"}).merge(daily[["day", "aircraft", "uptime"]], on="day", how="left"))
line = alt.Chart(rate).mark_line(strokeWidth=2, color=series[0], point=alt.OverlayMarkDef(size=40)).encode(
    x=alt.X("day:T", title=None, axis=alt.Axis(format="%-d %b", grid=False)),
    y=alt.Y("rate:Q", title="Aircraft per listening hour", scale=alt.Scale(zero=True)),
    tooltip=[alt.Tooltip("day:T", title="Day", format="%A %-d %B"),
             alt.Tooltip("rate:Q", title="Aircraft per listening hour (average)", format=".1f"),
             alt.Tooltip("busiest:Q", title="Busiest hour", format=".0f"),
             alt.Tooltip("hours:Q", title="Hours counted (80%+ uptime)"),
             alt.Tooltip("aircraft:Q", title="Aircraft heard that day", format=","),
             alt.Tooltip("uptime:Q", title="Receiver uptime", format=".0%")])
st.altair_chart(line.properties(height=240), width="stretch",
                alt="Line of average aircraft per listening hour for each day")
table_view(rate[["day", "rate", "hours"]], "Average aircraft per listening hour for each day",
           day=st.column_config.DateColumn("Day", format="ddd D MMM"),
           rate=st.column_config.NumberColumn("Aircraft per listening hour", format="%.1f"),
           hours="Hours counted")
definition("Definition: receiver uptime", metric="receiver_uptime")

# When is it busy?
st.header("Busiest hours", divider="gray")
cells = good.groupby(["weekday_pt", "hour_pt"], as_index=False).agg(
    rate=("aircraft_per_listening_hour", "mean"), hours=("hour_start", "size"),
    low=("aircraft_per_listening_hour", "min"), high=("aircraft_per_listening_hour", "max"))


def clock(hour: int) -> str:
    return f"{(hour % 12) or 12} {'am' if hour < 12 else 'pm'}"


cells["hours_label"] = [f"{clock(h)} to {clock((h + 1) % 24)}" for h in cells.hour_pt]
cells["range"] = [f"{lo:.0f} to {hi:.0f}" for lo, hi in zip(cells.low, cells.high)]
heat_range = ["#104281", "#86b6ef"] if dark else ["#cde2fb", "#0d366b"]
heat = alt.Chart(cells).mark_rect(stroke=SURFACE, strokeWidth=1).encode(
    x=alt.X("hour_pt:O", title="Hour of day (Pacific)", axis=alt.Axis(labelAngle=0)),
    y=alt.Y("weekday_pt:N", title=None, sort=WEEKDAYS, axis=alt.Axis(labelOverlap=False)),
    color=alt.Color("rate:Q", title="Aircraft per hour", scale=alt.Scale(range=heat_range),
                    legend=alt.Legend(orient="top", gradientLength=200)),
    tooltip=[alt.Tooltip("weekday_pt:N", title="Day"), alt.Tooltip("hours_label:N", title="Time (Pacific)"),
             alt.Tooltip("rate:Q", title="Aircraft per listening hour (average)", format=".1f"),
             alt.Tooltip("range:N", title="Lowest to highest"),
             alt.Tooltip("hours:Q", title="Hours averaged")])
st.altair_chart(heat.properties(height=260), width="stretch",
                alt="Heatmap of average aircraft per listening hour by weekday and hour of day")
st.caption(f"Each cell averages {cells.hours.min()} to {cells.hours.max()} hours, so single events "
           "still show; the pattern firms up as weeks accumulate.")
table_view(cells.pivot(index="weekday_pt", columns="hour_pt", values="rate").reindex(WEEKDAYS).round(1)
           .reset_index(), "Average aircraft per listening hour by weekday and hour",
           weekday_pt="Day")


def share_chart(df: pd.DataFrame, field: str, order: list[str], colors: list[str], title: str, alt_text: str) -> None:
    shares = df.groupby(["day", field], as_index=False).confirmed_aircraft.sum()
    shares["day_total"] = shares.groupby("day").confirmed_aircraft.transform("sum")
    shares["share"] = shares.confirmed_aircraft / shares.day_total
    chart = alt.Chart(shares).mark_bar(width=alt.RelativeBandSize(0.8), stroke=SURFACE, strokeWidth=1).encode(
        x=alt.X("yearmonthdate(day):T", title=None, axis=alt.Axis(format="%-d %b", grid=False)),
        y=alt.Y("confirmed_aircraft:Q", stack="normalize", title=title, axis=alt.Axis(format="%")),
        color=alt.Color(f"{field}:N", title=None, sort=order, scale=alt.Scale(domain=order, range=colors),
                        legend=alt.Legend(orient="top", columns=2)),  # fits a phone
        order=alt.Order("series_order:Q"),
        tooltip=[alt.Tooltip("day:T", title="Day", format="%A %-d %B"), alt.Tooltip(f"{field}:N", title="Group"),
                 alt.Tooltip("share:Q", title="Share of the day", format=".1%"),
                 alt.Tooltip("confirmed_aircraft:Q", title="Aircraft", format=","),
                 alt.Tooltip("day_total:Q", title="All aircraft that day", format=",")],
    ).transform_calculate(series_order=f"indexof({order}, datum.{field})")
    st.altair_chart(chart.properties(height=260), width="stretch", alt=alt_text)
    table_view(shares.pivot(index="day", columns=field, values="confirmed_aircraft").reindex(columns=order)
               .fillna(0).astype(int).reset_index(), alt_text,
               day=st.column_config.DateColumn("Day", format="ddd D MMM"))


st.header("What kinds of aircraft", divider="gray")
groups = {"Narrow-body / regional jet": "Airliner jets", "Wide-body jet": "Airliner jets",
          "Business jet": "Business jets", "Turboprop": "Turboprops", "Piston": "Piston aircraft",
          "Helicopter": "Helicopters"}
class_order = ["Airliner jets", "Business jets", "Turboprops", "Piston aircraft", "Helicopters", "Other or unknown"]
share_chart(mix.assign(group=mix.aircraft_class.map(groups).fillna("Other or unknown")), "group", class_order,
            series[:5] + [MUTED], "Share of aircraft", "Daily share of aircraft by class, as 100% stacked bars")
definition("Definition: aircraft class (in pub_aircraft)", model="pub_aircraft")

st.header("How aircraft were identified", divider="gray")
st.markdown("Each aircraft is identified by the most authoritative source that knows its transponder "
            "address: the FAA registry first, then the community and crowdsourced databases.")
sources = {"faa_registry": "FAA registry", "tar1090_db": "tar1090-db", "opensky": "OpenSky",
           "plane_alert_mil": "plane-alert-db"}
source_order = list(sources.values()) + ["Unidentified"]
share_chart(mix.assign(source=mix.identity_source.map(sources).fillna("Unidentified")), "source", source_order,
            series[:4] + [MUTED], "Share of aircraft", "Daily share of aircraft by identity source, as 100% stacked bars")
definition("Definition: identification rate", metric="identification_rate")

st.header("By band", divider="gray")
st.markdown("The receiver alternates between 1090 MHz (ADS-B, used by airliners and most aircraft) and "
            "978 MHz (UAT, used by some general aviation below 18,000 feet), so each band's rate is "
            "divided by the time spent on it.")
week = bands[bands.day > last - pd.Timedelta(days=7)].groupby("band_mhz").agg(
    aircraft=("confirmed_aircraft", "sum"), seconds=("listening_seconds", "sum"))
for column, (band, r) in zip(st.columns(2), week.sort_index(ascending=False).iterrows()):
    column.metric(f"{band} MHz: aircraft per listening hour, last 7 days", f"{r.aircraft / (r.seconds / 3600):.1f}",
                  help="Each day's confirmed aircraft on the band, summed, divided by the hours spent listening "
                       f"on it ({r.seconds / 3600:.0f} hours).", border=True)
definition("Definition: listening hours", metric="listening_hours")
