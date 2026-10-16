resource "google_artifact_registry_repository" "images" {
  repository_id = "sky-scanner"
  format        = "DOCKER"
  location      = var.region
  description   = "Container images for Cloud Run services and jobs."

  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }
}

resource "google_artifact_registry_repository_iam_member" "deploy_push" {
  repository = google_artifact_registry_repository.images.name
  location   = var.region
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.this["ci-deploy"].email}"
}

# The value is added by hand (never in Terraform state):
#   gcloud secrets versions add openrouter-api-key --data-file=-
resource "google_secret_manager_secret" "openrouter" {
  secret_id = "openrouter-api-key"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_iam_member" "notable_reads_openrouter" {
  secret_id = google_secret_manager_secret.openrouter.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.this["notable"].email}"
}

resource "google_monitoring_notification_channel" "email" {
  display_name = "Sky Scanner alerts"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }
}

# Hard daily ceiling on bytes scanned by any one principal (the public web
# and MCP accounts included), on top of maximum_bytes_billed per query.
# The default is unlimited.
resource "google_cloud_quotas_quota_preference" "query_per_user" {
  parent        = "projects/${var.project_id}"
  name          = "bigquery-query-usage-per-user-per-day"
  service       = "bigquery.googleapis.com"
  quota_id      = "QueryUsagePerUserPerDay"
  contact_email = var.alert_email
  justification = "Cost ceiling for public-facing service accounts."

  # Lowering from "unlimited" is always a >10% decrease, which the API
  # rejects unless this check is explicitly skipped.
  ignore_safety_checks = "QUOTA_DECREASE_PERCENTAGE_TOO_HIGH"

  quota_config {
    preferred_value = var.query_quota_mib_per_user_per_day
  }
}
