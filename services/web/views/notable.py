"""Notable aircraft: the weekly edition written by an AI agent through the
MCP server, with every sentence verified against the data it cites."""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from lib import bq
from lib.fmt import local

st.set_page_config(layout="centered")  # an article: keep lines readable


def as_json(value):
    return json.loads(value) if isinstance(value, str) else value


@st.cache_data(ttl=600, show_spinner="Loading editions…")
def editions() -> pd.DataFrame:
    return bq.run(f"""
        select edition_id, week_start, week_end, generated_at, purpose, status, model,
               to_json_string(edition) as edition, to_json_string(photos) as photos,
               statements_checked, statements_verified, verification_rate,
               to_json_string(removed_statements) as removed_statements,
               to_json_string(tool_calls) as tool_calls, tool_call_count, turns,
               prompt_tokens, completion_tokens, cost_usd, seconds
        from {bq.table("pub_notable_editions")}
        where status in ('published', 'eval')
        order by week_end desc, generated_at desc""")


df = editions()
if df.empty:
    st.title("Notable aircraft")
    st.info("The first weekly edition has not been written yet.", title="No editions yet")
    st.stop()

published = df[df.status == "published"]
pool = published if len(published) else df
labels = {
    row.edition_id: f"{row.week_start:%-d %b} to {(row.week_end - pd.Timedelta(days=1)):%-d %b %Y}"
    + ("" if row.status == "published" else " (evaluation edition)")
    for row in pool.drop_duplicates("week_end").itertuples()
}
header = st.container()  # filled once the week is chosen, but shown above the controls
controls = st.container(horizontal=True, vertical_alignment="bottom")
chosen = controls.selectbox("Week", list(labels), format_func=labels.get, width=260)
show_evidence = controls.toggle(
    "Show evidence", help="Under each sentence, show the tool call and the values it was checked against."
)
row = pool[pool.edition_id == chosen].iloc[0]
edition, photos = as_json(row.edition), as_json(row.photos) or {}


@st.cache_data(ttl=3600, show_spinner=False)
def registrations(addresses: tuple[str, ...]) -> dict[str, str]:
    """Address -> registration, to label links to aircraft pages."""
    if not addresses:
        return {}
    found = bq.run(
        f"""select address, registration from {bq.table("pub_aircraft")}
                       where address_type = 'icao' and address in unnest(split(@addresses))
                         and registration is not null""",
        addresses=",".join(addresses),
    )
    return dict(zip(found.address, found.registration))


names = registrations(
    tuple(
        sorted(
            {
                a.upper()
                for sec in edition.get("sections", [])
                for e in sec.get("entries", [])
                for a in (e.get("addresses") or [])
            }
        )
    )
)


def statements(items: list[dict] | None) -> None:
    for s in items or []:
        st.markdown(s["text"])
        if show_evidence:
            st.caption("; ".join(f"`{e['call']}` {e['path']} = {e['value']!r}" for e in s["evidence"]))


header.title(edition.get("title") or "Notable aircraft")
if edition.get("subtitle"):
    header.caption(edition["subtitle"])
if row.status != "published":
    st.warning(
        "This edition was written to evaluate the agent and is not a published weekly edition.",
        title="Evaluation edition",
    )

statements(edition.get("summary"))

for section in edition.get("sections", []):
    st.header(section["heading"], divider="gray")
    statements(section.get("intro"))
    for entry in section.get("entries", []):
        with st.container(border=True):
            photo = photos.get(entry.get("designator") or "")
            left, right = st.columns([1, 2], gap="medium")
            with left:
                if photo and photo.get("image_url"):
                    credit = photo.get("credit") or {}
                    who = ", ".join(x for x in (credit.get("author"), credit.get("license")) if x)
                    caption = f"Photo: {who}, via Wikimedia Commons." if who else "Photo via Wikimedia Commons."
                    caption += " A representative aircraft of the type, not necessarily this one."
                    st.image(photo["image_url"], caption=caption, alt=f"Photo of a {entry['title']}")
            with right:
                st.subheader(entry["title"])
                if entry.get("what_it_is"):
                    st.markdown(f"*{entry['what_it_is']['text']}*")
                    if show_evidence:
                        st.caption(
                            "; ".join(
                                f"`{e['call']}` {e['path']} = {e['value']!r}" for e in entry["what_it_is"]["evidence"]
                            )
                        )
                statements(entry.get("details"))
                links = st.container(horizontal=True, gap="small")
                for address in (entry.get("addresses") or [])[:8]:
                    links.page_link(
                        "views/aircraft_detail.py",
                        label=names.get(address.upper(), address.upper()),
                        icon=":material/flight:",
                        query_params={"address": address.upper()},
                        help="Open this aircraft's page",
                    )
                if photo and photo.get("article_url"):
                    links.link_button(
                        f"Wikipedia: {photo.get('article_title') or entry['title']}",
                        photo["article_url"],
                        type="tertiary",
                        icon=":material/open_in_new:",
                    )

if edition.get("privacy"):
    st.header("Privacy-protected aircraft", divider="gray")
    statements(edition["privacy"])
if edition.get("caveats"):
    st.header("Data-quality caveats", divider="gray")
    for s in edition["caveats"]:
        st.markdown(f"- {s['text']}")

st.header("Sources", divider="gray")
st.markdown(
    "Data comes from the Sky Scanner receiver on 1090 MHz and 978 MHz. Aircraft were identified from the "
    "FAA aircraft registry, tar1090-db, the OpenSky aircraft database (crowdsourced) and plane-alert-db. "
    "Photos are the lead images of the linked English Wikipedia articles, hosted on Wikimedia Commons; "
    "credits and licenses are under each photo."
)

with st.expander("How this edition was made", icon=":material/smart_toy:"):
    st.markdown(
        f"An AI agent (`{row.model}` via OpenRouter) investigated the week through this site's "
        "[MCP server](https://mcp.skyscanner.pharrhomelab.com/mcp), using the same public data and "
        "definitions as the rest of the site. It had to cite a tool call for every number, time and name. "
        "A program then checked every cited value against what the tool actually returned and removed "
        "any sentence that did not match."
    )
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Sentences verified", f"{row.statements_verified} of {row.statements_checked}")
    c2.metric("Tool calls", int(row.tool_call_count))
    c3.metric("Cost", f"${row.cost_usd:.2f}")
    c4.metric("Written in", f"{row.seconds / 60:.0f} min")
    st.caption(
        f"Generated {local(pd.Series([row.generated_at])).iloc[0]:%Y-%m-%d %H:%M} PT, using "
        f"{row.prompt_tokens:,} prompt and {row.completion_tokens:,} completion tokens."
    )
    removed = as_json(row.removed_statements) or []
    if removed:
        st.markdown(
            f"**Removed by the verifier ({len(removed)})**: shown for transparency; their content is not verified."
        )
        st.dataframe(
            pd.DataFrame(removed)[["where", "text", "reason"]],
            hide_index=True,
            width="stretch",
            alt="Sentences the verifier removed, with the reason",
        )
    calls = pd.DataFrame(as_json(row.tool_calls) or [])
    if len(calls):
        st.markdown("**Tool calls**")
        st.dataframe(calls, hide_index=True, width="stretch", alt="The agent's tool calls in order")
