# One service account per component, each with only the access it needs.
locals {
  service_accounts = {
    edge      = "Home replicator: writes raw tables"
    dbt       = "dbt runner: reads raw, builds analytics and public"
    web       = "Streamlit website: reads public"
    mcp       = "MCP server: reads public"
    notable   = "Notable-aircraft agent: reads public, writes ai"
    ci-plan   = "GitHub Actions on pull requests: terraform plan, dbt CI"
    ci-deploy = "GitHub Actions on main: push images, deploy Cloud Run"
    scheduler = "Cloud Scheduler: starts the scheduled Cloud Run jobs"
  }

  # dataset => { service account => legacy role }. Legacy READER/WRITER avoid
  # the perpetual diffs predefined roles cause on dataset access entries.
  dataset_grants = {
    raw       = { edge = "WRITER", dbt = "READER", ci-plan = "READER" }
    analytics = { dbt = "WRITER" }
    ai        = { notable = "WRITER", dbt = "READER" }
    public    = { dbt = "WRITER", web = "READER", mcp = "READER", notable = "READER" }
  }

  project_roles = {
    edge      = ["roles/bigquery.jobUser"]
    dbt       = ["roles/bigquery.jobUser", "roles/logging.logWriter"]
    web       = ["roles/bigquery.jobUser", "roles/bigquery.readSessionUser"]
    mcp       = ["roles/bigquery.jobUser", "roles/bigquery.readSessionUser"]
    notable   = ["roles/bigquery.jobUser", "roles/bigquery.readSessionUser", "roles/logging.logWriter"]
    ci-plan   = ["roles/viewer", "roles/iam.securityReviewer", "roles/bigquery.user"]
    ci-deploy = ["roles/run.developer"]
  }
}

resource "google_service_account" "this" {
  for_each = local.service_accounts

  account_id   = "sky-${each.key}"
  display_name = each.value
}

resource "google_bigquery_dataset_access" "grants" {
  for_each = merge([
    for ds, grants in local.dataset_grants : {
      for sa, role in grants : "${ds}/${sa}" => { dataset = ds, sa = sa, role = role }
    }
  ]...)

  dataset_id    = google_bigquery_dataset.this[each.value.dataset].dataset_id
  role          = each.value.role
  user_by_email = google_service_account.this[each.value.sa].email
}

resource "google_project_iam_member" "roles" {
  for_each = merge([
    for sa, roles in local.project_roles : {
      for role in roles : "${sa}/${role}" => { sa = sa, role = role }
    }
  ]...)

  project = var.project_id
  role    = each.value.role
  member  = "serviceAccount:${google_service_account.this[each.value.sa].email}"
}

# The deployer may run Cloud Run services and jobs as the runtime accounts.
resource "google_service_account_iam_member" "deploy_act_as" {
  for_each = toset(["dbt", "web", "mcp", "notable"])

  service_account_id = google_service_account.this[each.key].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.this["ci-deploy"].email}"
}

# terraform plan on pull requests reads the state bucket.
resource "google_storage_bucket_iam_member" "plan_reads_state" {
  bucket = "${var.project_id}-tfstate"
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.this["ci-plan"].email}"
}

# The owner may act as the public-facing accounts, to verify the access tiers
# by querying as them (e.g. that sky-web cannot read analytics).
resource "google_service_account_iam_member" "owner_tests_public_accounts" {
  for_each = toset(["web", "mcp", "notable"])

  service_account_id = google_service_account.this[each.key].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "user:${var.owner_email}"
}
