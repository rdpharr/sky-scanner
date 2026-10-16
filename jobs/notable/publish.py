"""Store editions in BigQuery (ai.notable_editions) with photo credits."""

from __future__ import annotations

import json
import re
import tempfile
import urllib.parse
import urllib.request
from typing import Any

from google.cloud import bigquery

EDITION_SCHEMA = [
    ("edition_id", "STRING"), ("week_start", "DATE"), ("week_end", "DATE"),
    ("generated_at", "TIMESTAMP"), ("purpose", "STRING"), ("model", "STRING"),
    ("status", "STRING"), ("edition", "JSON"), ("draft", "JSON"), ("verification", "JSON"),
    ("photos", "JSON"), ("trace", "JSON"), ("tool_calls", "INT64"), ("turns", "INT64"),
    ("prompt_tokens", "INT64"), ("completion_tokens", "INT64"), ("cost_usd", "FLOAT64"),
    ("seconds", "FLOAT64"), ("final_text", "STRING"), ("finish_reason", "STRING"),
    ("outline", "JSON"),
]


def commons_credit(file_url: str) -> dict[str, str] | None:
    """Author and license of a Wikimedia Commons file, from its metadata."""
    match = re.search(r"/wiki/(File:[^?#]+)", file_url or "")
    if not match:
        return None
    title = urllib.parse.unquote(match.group(1))
    url = ("https://commons.wikimedia.org/w/api.php?action=query&format=json&prop=imageinfo"
           "&iiprop=extmetadata&titles=" + urllib.parse.quote(title))
    request = urllib.request.Request(url, headers={"User-Agent": "SkyScanner/1.0 (notable edition credits)"})
    with urllib.request.urlopen(request, timeout=30) as response:
        pages = json.load(response)["query"]["pages"]
    meta = next(iter(pages.values())).get("imageinfo", [{}])[0].get("extmetadata", {})

    def text(key: str) -> str:
        return re.sub(r"<[^>]+>", "", meta.get(key, {}).get("value", "")).strip()

    return {"author": text("Artist"), "license": text("LicenseShortName"), "file_url": file_url}


def photos(client: bigquery.Client, dataset: str, designators: list[str]) -> dict[str, Any]:
    if not designators:
        return {}
    rows = client.query(
        f"""select designator, image_url, image_file_url, wikipedia_article_url, wikipedia_title
            from `{client.project}.{dataset}.pub_aircraft_types` where designator in unnest(@d)""",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ArrayQueryParameter("d", "STRING", sorted(set(designators)))])).result()
    out = {}
    for r in rows:
        if not r.image_url:
            continue
        try:
            credit = commons_credit(r.image_file_url)
        except Exception:  # a missing credit never blocks publishing
            credit = None
        out[r.designator] = {"image_url": r.image_url, "article_url": r.wikipedia_article_url,
                             "article_title": r.wikipedia_title, "credit": credit}
    return out


def save(client: bigquery.Client, dataset: str, row: dict[str, Any]) -> None:
    table = f"{client.project}.{dataset}.notable_editions"
    config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=[bigquery.SchemaField(n, t) for n, t in EDITION_SCHEMA],
        write_disposition="WRITE_APPEND",
        schema_update_options=[bigquery.SchemaUpdateOption.ALLOW_FIELD_ADDITION])
    with tempfile.TemporaryFile() as f:
        f.write(json.dumps({n: row.get(n) for n, _ in EDITION_SCHEMA}, default=str).encode() + b"\n")
        f.seek(0)
        client.load_table_from_file(f, table, job_config=config).result()
