"""Small display helpers shared by pages."""

from __future__ import annotations

import pandas as pd

LOCAL_TZ = "America/Los_Angeles"

# One colour per aircraft kind, used on maps and charts.
KIND_COLORS = {
    "Military": [214, 69, 65],
    "Airline": [52, 120, 198],
    "Other": [242, 159, 5],
    None: [140, 140, 140],
}


def local(ts: pd.Series) -> pd.Series:
    return pd.to_datetime(ts, utc=True).dt.tz_convert(LOCAL_TZ)


def local_naive(ts: pd.Series) -> pd.Series:
    """Pacific wall-clock times without a zone, so charts show Pacific time
    whatever the viewer's own time zone."""
    return local(ts).dt.tz_localize(None)


def present(value) -> bool:
    """True for a real value: not None, NaN, pandas NA/NaT or an empty string."""
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):  # arrays and lists are values
        return True
    return not (isinstance(value, str) and value == "")


def ago(seconds: float | None) -> str:
    if seconds is None or pd.isna(seconds):
        return "never"
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds} s ago"
    if seconds < 5400:
        return f"{seconds // 60} min ago"
    if seconds < 172800:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} days ago"
