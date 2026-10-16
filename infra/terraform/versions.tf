terraform {
  required_version = ">= 1.9"

  backend "gcs" {
    bucket = "sky-scanner-pharr-tfstate"
    prefix = "core"
  }

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.0, < 9.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.6, < 4.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region

  # Bill API calls to this project. Some APIs (Cloud Quotas) reject user
  # credentials without an explicit quota project.
  user_project_override = true
  billing_project       = var.project_id
}

data "google_project" "this" {}
