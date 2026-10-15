# Sky Scanner

A home ADS-B/UAT receiver feeding a public analytics stack on BigQuery, dbt
and Streamlit, with data quality alerting and an MCP server.

This repository is being reorganized; see [docs/PLAN.md](docs/PLAN.md).

| Directory | Contents |
|---|---|
| [edge/](edge/README.md) | The receiver, ingestion, enrichment and local Postgres that run at home |
| [infra/](infra/) | Google Cloud bootstrap and (soon) Terraform |
| [docs/](docs/) | Plan, architecture and decision records |
