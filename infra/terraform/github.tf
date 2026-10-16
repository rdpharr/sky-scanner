# Keyless GitHub Actions authentication (Workload Identity Federation).
# Only workflows from var.github_repo can exchange tokens; only the main
# branch can act as the deployer.
resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-oidc"
  display_name                       = "GitHub OIDC"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }
  attribute_condition = "assertion.repository == '${var.github_repo}'"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

locals {
  wif_pool = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}"
}

resource "google_service_account_iam_member" "ci_plan_wif" {
  service_account_id = google_service_account.this["ci-plan"].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "${local.wif_pool}/attribute.repository/${var.github_repo}"
}

resource "google_service_account_iam_member" "ci_deploy_wif" {
  service_account_id = google_service_account.this["ci-deploy"].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "${local.wif_pool}/attribute.ref/refs/heads/main"
}
