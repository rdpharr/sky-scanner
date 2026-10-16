"""Sky Scanner public website (Streamlit on Cloud Run)."""

import streamlit as st

st.set_page_config(page_title="Sky Scanner", page_icon="✈️")  # layout is set per page

pages = st.navigation({
    "": [
        st.Page("views/live.py", title="Live", icon=":material/radar:", default=True),
        st.Page("views/trends.py", title="Trends", icon=":material/trending_up:"),
        st.Page("views/notable.py", title="Notable this week", icon=":material/star:"),
        st.Page("views/aircraft.py", title="Aircraft log", icon=":material/flight:"),
        st.Page("views/about.py", title="About", icon=":material/info:"),
        # Opened from links (Aircraft log, Live, Notable), not from the menu.
        st.Page("views/aircraft_detail.py", title="Aircraft", icon=":material/flight:",
                url_path="aircraft-detail", visibility="hidden"),
    ],
    # How the numbers are defined, checked and evaluated: reference pages.
    "Methods": [
        st.Page("views/definitions.py", title="Definitions", icon=":material/menu_book:"),
        st.Page("views/health.py", title="Data health", icon=":material/monitor_heart:"),
        st.Page("views/evals.py", title="Agent evals", icon=":material/science:"),
    ],
})
pages.run()
