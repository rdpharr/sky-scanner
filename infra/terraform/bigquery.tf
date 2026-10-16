# Datasets follow the access tiers in docs/PLAN.md §4.2 and §4.5:
#   raw        replicated edge tables, written only by the edge replicator
#   analytics  dbt staging/intermediate/marts; never readable by public services
#   ai         notable-aircraft editions and eval results, written by the notable job
#   public     dbt-built views; the only dataset web, MCP and the agent can read
locals {
  datasets = {
    raw       = "Tables replicated from the home receiver's Postgres, unmodified."
    analytics = "dbt staging, intermediate and mart models. Not readable by public services."
    ai        = "Notable-aircraft editions, agent traces and evaluation results."
    public    = "Views exposed to the website, MCP server and agent. LADD/PIA aircraft and private owners' names removed."
  }
}

resource "google_bigquery_dataset" "this" {
  for_each = local.datasets

  dataset_id  = each.key
  description = each.value
  location    = var.bq_location

  delete_contents_on_destroy = false
}

# Views in `public` may read the datasets they are built from without their
# readers getting access to those datasets (authorized dataset).
resource "google_bigquery_dataset_access" "public_views" {
  for_each = toset(["analytics", "ai"])

  dataset_id = google_bigquery_dataset.this[each.key].dataset_id
  dataset {
    dataset {
      project_id = var.project_id
      dataset_id = google_bigquery_dataset.this["public"].dataset_id
    }
    target_types = ["VIEWS"]
  }
}
