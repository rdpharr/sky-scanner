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
