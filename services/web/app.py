"""Sky Scanner public website (Streamlit on Cloud Run)."""

import streamlit as st

st.set_page_config(page_title="Sky Scanner", page_icon="✈️", layout="wide")

pages = st.navigation([
    st.Page("views/live.py", title="Live", icon="📡", default=True),
    st.Page("views/notable.py", title="Notable this week", icon="⭐"),
    st.Page("views/aircraft.py", title="Aircraft log", icon="🛩️"),
    st.Page("views/definitions.py", title="Definitions", icon="📖"),
    st.Page("views/evals.py", title="Agent evals", icon="🧪"),
    st.Page("views/health.py", title="Data health", icon="✅"),
    st.Page("views/about.py", title="About", icon="ℹ️"),
])
pages.run()
