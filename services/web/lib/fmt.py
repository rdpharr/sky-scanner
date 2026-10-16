"""Small display helpers shared by pages."""

from __future__ import annotations

import pandas as pd

LOCAL_TZ = "America/Los_Angeles"

# One colour per aircraft kind, used on maps and charts. Checked with the
# dataviz palette validator on Streamlit's light and dark backgrounds
# (colour-blind separation, lightness band, 3:1 contrast).
KIND_COLORS = {
    "Military": [214, 69, 65],
    "Airline": [52, 120, 198],
    "Other": [201, 133, 0],
    None: [140, 140, 140],
}
KIND_HEX = {k: "#{:02x}{:02x}{:02x}".format(*v) for k, v in KIND_COLORS.items() if k}

# Categorical series colours in a fixed order (never cycled), with steps
# for each theme. The order is the one validated for adjacent series.
SERIES = {
    "light": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"],
    "dark": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300"],
}
MUTED = "#898781"  # de-emphasis grey for "Unidentified" and context marks


def detail_url(address: str, address_type: str, *labels) -> str:
    """Link to an aircraft's page. The text after '#' is what link columns
    display (the first known label, else the address); browsers do not send it."""
    label = next((x for x in labels if present(x)), address)
    query = f"address={address}" + ("" if address_type == "icao" else f"&type={address_type}")
    return f"/aircraft-detail?{query}#{label}"


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
