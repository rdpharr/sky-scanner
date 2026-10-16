# Email alerts. Error lines come from jobs/dbt_runner/run.py (one structured
# log line per failed check); warnings are not emailed and appear on the
# website's Data health page instead.
locals {
  dbt_log_filter = <<-EOT
    resource.type="cloud_run_job"
    resource.labels.job_name=~"^dbt-"
    severity>=ERROR
  EOT
  runbook        = "https://github.com/${var.github_repo}/blob/main/docs/runbook.md"
}

resource "google_monitoring_alert_policy" "data_check_failed" {
  display_name          = "Sky Scanner: data quality check failed"
  combiner              = "OR"
  notification_channels = [google_monitoring_notification_channel.email.name]

  conditions {
    display_name = "A dbt test or model failed"
    condition_matched_log {
      filter = "${local.dbt_log_filter} jsonPayload.event=\"dbt_node_failed\" jsonPayload.resource_type!=\"source\""
      label_extractors = {
        check  = "EXTRACT(jsonPayload.name)"
        status = "EXTRACT(jsonPayload.status)"
      }
    }
  }

  alert_strategy {
    notification_rate_limit { period = "300s" }
    auto_close = "1800s"
  }

  documentation {
    mime_type = "text/markdown"
    subject   = "Sky Scanner: data check $${log.extracted_label.check} $${log.extracted_label.status}"
    content   = <<-EOT
      A scheduled dbt run found a failing test or a model that did not build.
      The log entry names the check and the number of failing rows.

      - Failing rows: query the test's SQL in BigQuery (`dbt test -s <check> --store-failures`).
      - Model errors: the dbt output in the job's logs has the SQL error.
      - Runbook: ${local.runbook}
    EOT
  }
}

resource "google_monitoring_alert_policy" "source_stale" {
  display_name          = "Sky Scanner: data stopped arriving"
  combiner              = "OR"
  notification_channels = [google_monitoring_notification_channel.email.name]

  conditions {
    display_name = "A source is past its freshness limit"
    condition_matched_log {
      filter = "${local.dbt_log_filter} jsonPayload.event=\"dbt_node_failed\" jsonPayload.resource_type=\"source\""
      label_extractors = {
        source = "EXTRACT(jsonPayload.name)"
      }
    }
  }

  alert_strategy {
    notification_rate_limit { period = "300s" }
    auto_close = "1800s"
  }

  documentation {
    mime_type = "text/markdown"
    subject   = "Sky Scanner: no new $${log.extracted_label.source} data"
    content   = <<-EOT
      A raw table in BigQuery has had no new rows for longer than its limit.

      - `capture_intervals` stale (15 min): the receiver process, the edge
        database or the replicator is down. Check `docker compose ps` in `edge/`.
      - `aircraft_snapshots` stale (60 min) while capture intervals are fresh:
        the radio is listening but decoding nothing. Check the antenna and
        that the PortaPack is in HackRF mode.
      - `aircraft_metadata` stale (50 h): the replicator's daily reload failed.

      Runbook: ${local.runbook}
    EOT
  }
}

resource "google_monitoring_alert_policy" "job_failed" {
  display_name          = "Sky Scanner: scheduled job failed"
  combiner              = "OR"
  notification_channels = [google_monitoring_notification_channel.email.name]

  conditions {
    display_name = "A Cloud Run job execution failed"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_job\" AND metric.type = \"run.googleapis.com/job/completed_execution_count\" AND metric.labels.result = \"failed\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  alert_strategy {
    auto_close = "3600s"
  }

  documentation {
    mime_type = "text/markdown"
    content   = <<-EOT
      A Cloud Run job execution failed: dbt could not run at all (bad
      credentials, a broken image, a timeout). Check the execution's logs.
      Runbook: ${local.runbook}
    EOT
  }
}

resource "google_monitoring_alert_policy" "freshness_checks_stopped" {
  display_name          = "Sky Scanner: freshness checks stopped running"
  combiner              = "OR"
  notification_channels = [google_monitoring_notification_channel.email.name]

  conditions {
    display_name = "No successful dbt-freshness run for an hour"
    condition_absent {
      filter   = "resource.type = \"cloud_run_job\" AND resource.labels.job_name = \"dbt-freshness\" AND metric.type = \"run.googleapis.com/job/completed_execution_count\" AND metric.labels.result = \"succeeded\""
      duration = "3600s"
      aggregations {
        alignment_period   = "900s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  alert_strategy {
    auto_close = "3600s"
  }

  documentation {
    mime_type = "text/markdown"
    content   = <<-EOT
      The freshness job has not succeeded for an hour, so "data stopped
      arriving" alerts cannot fire. Check Cloud Scheduler (job dbt-freshness)
      and the job's recent executions. Runbook: ${local.runbook}
    EOT
  }
}
