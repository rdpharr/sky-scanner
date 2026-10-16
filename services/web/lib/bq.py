"""BigQuery access for the website.

The site reads only the `public` dataset (the sky-web service account can
read nothing else). Every query has a byte cap, and results are cached so a
page view rarely reaches BigQuery: most queries bill the 10 MB minimum.
"""

from __future__ import annotations

import os

import pandas as pd
import streamlit as st
from google.cloud import bigquery

PROJECT = os.environ.get("GCP_PROJECT", "sky-scanner-pharr")
DATASET = os.environ.get("PUBLIC_DATASET", "public")
MAX_BYTES_BILLED = int(os.environ.get("MAX_BYTES_BILLED", 500_000_000))


@st.cache_resource
def client() -> bigquery.Client:
    return bigquery.Client(project=PROJECT)


def table(name: str) -> str:
    return f"`{PROJECT}.{DATASET}.{name}`"


def _param(name: str, value) -> bigquery.ScalarQueryParameter:
    kind = {bool: "BOOL", int: "INT64", float: "FLOAT64"}.get(type(value), "STRING")
    if hasattr(value, "isoformat"):
        kind = "TIMESTAMP"
    return bigquery.ScalarQueryParameter(name, kind, value)


def run(sql: str, **params) -> pd.DataFrame:
    """Run a parameterized query (no caching). Use @name placeholders."""
    config = bigquery.QueryJobConfig(
        maximum_bytes_billed=MAX_BYTES_BILLED,
        query_parameters=[_param(k, v) for k, v in params.items()],
        labels={"component": "web"},
    )
    return client().query(sql, job_config=config).to_dataframe()


@st.cache_data(ttl=15, show_spinner=False)
def query_15s(sql: str, **params) -> pd.DataFrame:
    return run(sql, **params)


@st.cache_data(ttl=300, show_spinner=False)
def query_5m(sql: str, **params) -> pd.DataFrame:
    return run(sql, **params)


@st.cache_data(ttl=3600, show_spinner=False)
def query_1h(sql: str, **params) -> pd.DataFrame:
    return run(sql, **params)
