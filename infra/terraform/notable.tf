# Weekly notable-aircraft edition (jobs/notable): an OpenRouter model works
# through the public MCP server; its edition is verified and stored in ai.
resource "google_cloud_run_v2_job" "notable" {
  name                = "notable-edition"
  location            = var.region
  deletion_protection = false
  labels              = { component = "notable" }

  template {
    task_count = 1
    template {
      service_account = google_service_account.this["notable"].email
      timeout         = "3600s"
      max_retries     = 0
      containers {
        image = var.notable_image
        args  = ["edition"]
        env {
          name  = "GCP_PROJECT"
          value = var.project_id
        }
        env {
          name  = "MCP_URL"
          value = "https://${var.mcp_domain}/mcp"
        }
        env {
          name = "OPENROUTER_API_KEY"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.openrouter.secret_id
              version = "latest"
            }
          }
        }
        resources {
          limits = { cpu = "1", memory = "1Gi" }
        }
      }
    }
  }

  depends_on = [google_secret_manager_secret_iam_member.notable_reads_openrouter]

  lifecycle {
    ignore_changes = [template[0].template[0].containers[0].image, client, client_version]
  }
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_runs_notable" {
  name     = google_cloud_run_v2_job.notable.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.this["scheduler"].email}"
}

resource "google_cloud_scheduler_job" "notable" {
  name             = "notable-edition"
  description      = "Weekly notable-aircraft edition, Mondays 06:00 Pacific, covering Monday to Sunday"
  schedule         = "0 6 * * 1"
  time_zone        = "America/Los_Angeles"
  attempt_deadline = "60s"

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/${google_cloud_run_v2_job.notable.id}:run"
    oauth_token {
      service_account_email = google_service_account.this["scheduler"].email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.scheduler_runs_notable]
}
