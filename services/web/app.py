"""Sky Scanner public website (Streamlit on Cloud Run)."""

import streamlit as st

st.set_page_config(page_title="Sky Scanner", page_icon="✈️")  # layout is set per page

# An open tab holds a websocket, which keeps a Cloud Run instance billed at the
# full rate. After IDLE_MINUTES without input, send the tab to a static page
# (static/paused.html) that closes the connection and offers to resume.
IDLE_MINUTES = 15
st.html(
    f"""<script>
(() => {{
  if (window.skyIdleWatch) return;
  window.skyIdleWatch = true;
  let last = Date.now();
  const seen = () => {{ last = Date.now(); }};
  for (const e of ["pointerdown", "pointermove", "keydown", "wheel", "scroll", "touchstart"]) {{
    window.addEventListener(e, seen, {{ passive: true, capture: true }});
  }}
  setInterval(() => {{
    if (Date.now() - last < {IDLE_MINUTES * 60_000}) return;
    const next = encodeURIComponent(location.pathname + location.search);
    location.replace("/app/static/paused.html?next=" + next);
  }}, 30_000);
}})();
</script>""",
    unsafe_allow_javascript=True,
)

pages = st.navigation(
    {
        "": [
            st.Page("views/live.py", title="Live", icon=":material/radar:", default=True),
            st.Page("views/trends.py", title="Trends", icon=":material/trending_up:"),
            st.Page("views/notable.py", title="Notable this week", icon=":material/star:"),
            st.Page("views/aircraft.py", title="Aircraft log", icon=":material/flight:"),
            st.Page("views/about.py", title="About", icon=":material/info:"),
            # Opened from links (Aircraft log, Live, Notable), not from the menu.
            st.Page(
                "views/aircraft_detail.py",
                title="Aircraft",
                icon=":material/flight:",
                url_path="aircraft-detail",
                visibility="hidden",
            ),
        ],
        # How the numbers are defined, checked and evaluated: reference pages.
        "Methods": [
            st.Page("views/definitions.py", title="Definitions", icon=":material/menu_book:"),
            st.Page("views/health.py", title="Data health", icon=":material/monitor_heart:"),
            st.Page("views/evals.py", title="Agent evals", icon=":material/science:"),
        ],
    }
)
pages.run()
