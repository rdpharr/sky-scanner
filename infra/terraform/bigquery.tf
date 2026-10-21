# Datasets are the access tiers (see docs/runbook.md for who runs as what):
#   raw        replicated edge tables, written only by the edge replicator
#   analytics  dbt staging/intermediate/marts; never readable by public services
#   ai         notable-aircraft editions and eval results, written by the notable job
#   public     dbt-built views; the only dataset web, MCP and the agent can read
locals {
  datasets = {
    raw       = "Tables replicated from the home receiver's Postgres, unmodified."
    analytics = "dbt staging, intermediate and mart models. Not readable by public services."
    ai        = "Notable-aircraft editions, agent traces and evaluation results."
    public    = "Views exposed to the website, MCP server and agent. Private owners' names removed; LADD/PIA aircraft flagged."
  }
}

resource "google_bigquery_dataset" "this" {
  for_each = local.datasets

  dataset_id  = each.key
  description = each.value
  location    = var.bq_location

  delete_contents_on_destroy = false
}

# Views in `public` may read the datasets they are built from (raw for the
# live view, analytics, ai) without their readers getting access to those
# datasets (authorized dataset).
resource "google_bigquery_dataset_access" "public_views" {
  for_each = toset(["raw", "analytics", "ai"])

  dataset_id = google_bigquery_dataset.this[each.key].dataset_id
  dataset {
    dataset {
      project_id = var.project_id
      dataset_id = google_bigquery_dataset.this["public"].dataset_id
    }
    target_types = ["VIEWS"]
  }
}

# Cloud Billing's standard usage cost export, written by Google. Terraform
# cannot turn the export on; it is enabled once in the console (see
# docs/runbook.md, Costs).
resource "google_bigquery_dataset" "billing" {
  dataset_id  = "billing"
  description = "Cloud Billing standard usage cost export, written by Google."
  location    = var.bq_location

  delete_contents_on_destroy = false
}
