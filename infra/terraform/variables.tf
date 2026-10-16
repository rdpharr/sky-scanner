variable "project_id" {
  type    = string
  default = "sky-scanner-pharr"
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "bq_location" {
  type    = string
  default = "US"
}

variable "github_repo" {
  description = "owner/name of the repository allowed to authenticate from GitHub Actions."
  type        = string
  default     = "rdpharr/sky-scanner"
}

# Kept out of the public repository: set it in terraform.tfvars (ignored).
variable "alert_email" {
  description = "Address that receives pipeline and data quality alerts."
  type        = string
}

variable "query_quota_mib_per_user_per_day" {
  description = "BigQuery bytes a single principal may scan per day, in MiB. A hard ceiling on query cost."
  type        = number
  default     = 102400 # 100 GiB
}

variable "dbt_runner_image" {
  description = "Image for the first deploy of the dbt jobs. Later images are deployed by CI; Terraform ignores the difference."
  type        = string
  default     = "us-central1-docker.pkg.dev/sky-scanner-pharr/sky-scanner/dbt-runner:phase3-1792112448"
}

variable "web_image" {
  description = "Image for the first deploy of the website. Later images are deployed by CI."
  type        = string
  default     = "us-central1-docker.pkg.dev/sky-scanner-pharr/sky-scanner/web:phase4-1792118058"
}

variable "web_domain" {
  description = "Custom domain for the website. Must be verified in Google Search Console by the account running Terraform; empty disables the mapping."
  type        = string
  default     = "skyscanner.pharrhomelab.com"
}

# Kept out of the public repository: set it in terraform.tfvars (ignored).
variable "owner_email" {
  description = "Google account of the project owner, allowed to impersonate the public-facing service accounts to test what they can read."
  type        = string
}

variable "mcp_image" {
  description = "Image for the first deploy of the MCP server. Later images are deployed by CI."
  type        = string
  default     = "us-central1-docker.pkg.dev/sky-scanner-pharr/sky-scanner/mcp:phase5-1792163270"
}

variable "mcp_domain" {
  description = "Custom domain for the MCP server (verified like web_domain); empty disables the mapping."
  type        = string
  default     = "mcp.skyscanner.pharrhomelab.com"
}
