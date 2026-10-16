"""Agent evaluations: is the AI that writes the notable page trustworthy?"""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from lib import bq
from lib.fmt import local

st.title("Agent evals")
st.markdown(
    "The weekly notable page is written by an AI agent working through this site's MCP server. "
    "These evaluations measure whether that is trustworthy, on a fixed past week so results do not drift:")
st.markdown(
    "- **Q&A accuracy**: factual questions answered by an agent using only the MCP tools, graded against "
    "SQL written directly on the same public data.\n"
    "- **Edition recall**: the share of items in a hand-checked reference report (26 September to "
    "2 October 2026) that the agent's edition for the same week covered.")


@st.cache_data(ttl=600, show_spinner="Loading evaluations…")
def runs() -> pd.DataFrame:
    return bq.run(f"""
        select run_id, kind, model, started_at, score, passed, total, cost_usd, seconds,
               to_json_string(details) as details
        from {bq.table('pub_eval_runs')} order by started_at desc""")


df = runs()
if df.empty:
    st.info("No evaluations have run yet.", title="No evaluations")
    st.stop()
df["ran"] = local(df.started_at).dt.strftime("%Y-%m-%d %H:%M")

st.header("Q&A accuracy", divider="gray")
qa = df[df.kind == "qa"]
st.dataframe(
    qa[["ran", "model", "score", "passed", "total", "cost_usd", "seconds"]], hide_index=True, width="stretch",
    alt="Q&A evaluation runs by model",
    column_config={"ran": "Ran (PT)", "score": st.column_config.ProgressColumn("Accuracy", min_value=0, max_value=1,
                                                                              format="percent"),
                   "cost_usd": st.column_config.NumberColumn("Cost", format="$%.3f"),
                   "seconds": st.column_config.NumberColumn("Seconds", format="%d")})
if len(qa):
    labels = {r.run_id: f"{r.model} · {r.ran}" for r in qa.itertuples()}
    chosen = st.selectbox("Questions for run", list(labels), format_func=labels.get)
    details = pd.DataFrame(json.loads(qa[qa.run_id == chosen].details.iloc[0]))
    details["tools"] = details.tool_calls.map(lambda calls: ", ".join(calls or []))
    st.dataframe(details[["passed", "question", "answer", "gold", "tools"]], hide_index=True, width="stretch",
                 alt="Each question with the agent's answer and the gold answer",
                 column_config={"passed": "Correct", "gold": "Gold (SQL)", "tools": "Tools used"})

st.header("Edition recall", divider="gray")
recall = df[df.kind == "edition_recall"]
if len(recall):
    st.dataframe(recall[["ran", "model", "score", "passed", "total"]], hide_index=True, width="stretch",
                 alt="Edition recall runs",
                 column_config={"ran": "Ran (PT)", "score": st.column_config.ProgressColumn(
                     "Recall", min_value=0, max_value=1, format="percent"),
                     "passed": "Items found", "total": "Reference items"})
    items = pd.DataFrame(json.loads(recall.details.iloc[0])["items"])
    st.dataframe(items, hide_index=True, width="stretch", alt="Reference items found or missed by the latest edition",
                 column_config={"item": "Reference item", "found": "Covered"})
