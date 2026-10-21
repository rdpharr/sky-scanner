# Public website (services/web): Streamlit on Cloud Run, reading only the
# BigQuery public dataset as sky-web.
resource "google_cloud_run_v2_service" "web" {
  name                = "web"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false
  labels              = { component = "web" }

  template {
    service_account = google_service_account.this["web"].email
    # Streamlit keeps a websocket per viewer: stick each viewer to one instance.
    # An open websocket keeps the instance billed at the full rate, so the
    # page lets idle tabs disconnect (app.py) and the timeout caps the rest.
    session_affinity                 = true
    timeout                          = "300s"
    max_instance_request_concurrency = 80

    scaling {
      # One warm instance avoids Streamlit's slow cold start for visitors.
      min_instance_count = 1
      max_instance_count = 3
    }

    containers {
      image = var.web_image
      ports {
        container_port = 8080
      }
      env {
        name  = "GCP_PROJECT"
        value = var.project_id
      }
      env {
        name  = "PUBLIC_DATASET"
        value = google_bigquery_dataset.this["public"].dataset_id
      }
      resources {
        limits   = { cpu = "1", memory = "1Gi" }
        cpu_idle = true
      }
    }
  }

  lifecycle {
    ignore_changes = [template[0].containers[0].image, client, client_version]
  }
}

resource "google_cloud_run_v2_service_iam_member" "web_public" {
  name     = google_cloud_run_v2_service.web.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_domain_mapping" "web" {
  count    = var.web_domain == "" ? 0 : 1
  name     = var.web_domain
  location = var.region

  metadata {
    namespace = var.project_id
  }
  spec {
    route_name = google_cloud_run_v2_service.web.name
  }
}

output "web_url" {
  value = google_cloud_run_v2_service.web.uri
}

output "web_domain_dns_records" {
  description = "DNS records to create (DNS-only, not proxied) for the custom domain."
  value       = var.web_domain == "" ? [] : google_cloud_run_domain_mapping.web[0].status[0].resource_records
}
