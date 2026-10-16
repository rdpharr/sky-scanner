output "service_accounts" {
  value = { for k, sa in google_service_account.this : k => sa.email }
}

output "workload_identity_provider" {
  description = "Value for google-github-actions/auth `workload_identity_provider`."
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "image_repository" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "alert_channel" {
  value = google_monitoring_notification_channel.email.name
}
