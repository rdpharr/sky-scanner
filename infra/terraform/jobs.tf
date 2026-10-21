# Scheduled dbt runs (jobs/dbt_runner). One image, three modes.
locals {
  dbt_jobs = {
    hourly    = { schedule = "5 * * * *", description = "dbt build except daily-tagged models" }
    daily     = { schedule = "35 6 * * *", description = "dbt build of everything" }
    freshness = { schedule = "*/30 * * * *", description = "dbt source freshness (receiver and replication alive)" }
  }
}

resource "google_cloud_run_v2_job" "dbt" {
  for_each = local.dbt_jobs

  name                = "dbt-${each.key}"
  location            = var.region
  deletion_protection = false
  labels              = { component = "dbt" }

  template {
    task_count = 1
    template {
      service_account = google_service_account.this["dbt"].email
      timeout         = "1800s"
      # A failing test is reported once through logs, not retried.
      max_retries = 0
      containers {
        image = var.dbt_runner_image
        args  = [each.key]
        resources {
          limits = { cpu = "1", memory = "1Gi" }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [template[0].template[0].containers[0].image, client, client_version]
  }
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_runs_dbt" {
  for_each = local.dbt_jobs

  name     = google_cloud_run_v2_job.dbt[each.key].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.this["scheduler"].email}"
}

resource "google_cloud_scheduler_job" "dbt" {
  for_each = local.dbt_jobs

  name             = "dbt-${each.key}"
  description      = each.value.description
  schedule         = each.value.schedule
  time_zone        = "Etc/UTC"
  attempt_deadline = "60s"

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/${google_cloud_run_v2_job.dbt[each.key].id}:run"
    oauth_token {
      service_account_email = google_service_account.this["scheduler"].email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.scheduler_runs_dbt]
}
