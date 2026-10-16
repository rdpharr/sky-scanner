"""Canonical definitions, exported from dbt after every build."""

import streamlit as st

from lib import bq
from lib.fmt import present

st.set_page_config(layout="centered")  # mostly prose: keep lines readable

st.title("Definitions")
st.markdown(
    "Every number on this site comes from one definition, written once in dbt and "
    "tested on every build. The same definitions are served to AI agents through the "
    "MCP server, so a person and an agent asking the same question get the same answer."
)

# Other pages link here with ?metric=<name> or ?model=<name> to open that entry.
wanted_metric = st.query_params.get("metric")
wanted_model = st.query_params.get("model")

defs = bq.query_1h(f"""
    select kind, name, model, label, description, metric_type, formula, filter
    from {bq.table("pub_definitions")}""")

st.header("Metrics")
for m in defs[defs.kind == "metric"].sort_values("label").itertuples():
    with st.expander(f"**{m.label}** (`{m.name}`)", expanded=m.name == wanted_metric):
        st.write(m.description)
        detail = f"Type: {m.metric_type}. Computed as `{m.formula}`."
        if present(m.filter):
            detail += f" Filter: `{m.filter}`."
        st.caption(detail)

st.header("Public data")
st.caption("Views in the `public` BigQuery dataset: the only data the website, MCP server and agent can read.")
models = defs[defs.kind == "model"].sort_values("name")
columns = defs[defs.kind == "column"]
for m in models.itertuples():
    with st.expander(f"`{m.name}`", expanded=m.name == wanted_model):
        st.write(m.description)
        cols = columns[columns.model == m.name][["name", "description"]]
        if len(cols):
            st.dataframe(
                cols, hide_index=True, width="stretch", column_config={"name": "Column", "description": "Meaning"}
            )
