terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# ---------------------------------------------------------------------------
# Enable required APIs
# ---------------------------------------------------------------------------

resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",
    "pubsub.googleapis.com",
    "cloudscheduler.googleapis.com",
    "secretmanager.googleapis.com",
    "firestore.googleapis.com",
  ])

  service            = each.value
  disable_on_destroy = false
}

# ---------------------------------------------------------------------------
# Firestore — dedup store for alert notifications (one per bucket-city-date)
# ---------------------------------------------------------------------------

resource "google_firestore_database" "elim_dedup" {
  name        = "elim-dedup"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"

  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------------------
# Service account — used by Cloud Scheduler and Pub/Sub to invoke Cloud Run
# ---------------------------------------------------------------------------

resource "google_service_account" "invoker" {
  account_id   = "${var.service_name}-invoker"
  display_name = "HF Elim Pub/Sub & Scheduler Invoker"

  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------------------
# Pub/Sub topic
# ---------------------------------------------------------------------------

resource "google_pubsub_topic" "hf_obs" {
  name = var.pubsub_topic

  message_retention_duration = "600s"

  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------------------
# Cloud Run service
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "elim" {
  name     = var.service_name
  location = var.region

  template {
    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    containers {
      image = var.image

      resources {
        limits = {
          cpu    = var.cpu
          memory = var.memory
        }
      }

      # Plain env vars
      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "PUBSUB_TOPIC"
        value = var.pubsub_topic
      }
      env {
        name  = "FIRESTORE_DATABASE"
        value = google_firestore_database.elim_dedup.name
      }

      # Secrets from Secret Manager
      env {
        name = "TELEGRAM_BOT_TOKEN"
        value_source {
          secret_key_ref {
            secret  = var.telegram_bot_token_secret
            version = "latest"
          }
        }
      }
      env {
        name = "POSTPEAK_ELIM_CHAT_ID"
        value_source {
          secret_key_ref {
            secret  = var.postpeak_elim_chat_id_secret
            version = "latest"
          }
        }
      }

      ports {
        container_port = 8080
      }
    }

    timeout = "${var.timeout_seconds}s"

    service_account = google_service_account.runtime.email
  }

  depends_on = [google_project_service.apis]
}

# Service account for the Cloud Run service itself (to access Secret Manager)
resource "google_service_account" "runtime" {
  account_id   = "${var.service_name}-runtime"
  display_name = "HF Elim Cloud Run Runtime"

  depends_on = [google_project_service.apis]
}

# Grant runtime SA access to secrets
resource "google_secret_manager_secret_iam_member" "tg_token" {
  secret_id = var.telegram_bot_token_secret
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_secret_manager_secret_iam_member" "tg_chat" {
  secret_id = var.postpeak_elim_chat_id_secret
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

# Grant runtime SA access to Firestore (dedup store)
resource "google_project_iam_member" "runtime_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

# Grant runtime SA permission to publish to Pub/Sub
resource "google_pubsub_topic_iam_member" "runtime_publisher" {
  topic  = google_pubsub_topic.hf_obs.name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${google_service_account.runtime.email}"
}

# ---------------------------------------------------------------------------
# IAM — allow invoker SA to call Cloud Run
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_service_iam_member" "invoker" {
  name     = google_cloud_run_v2_service.elim.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.invoker.email}"
}

# ---------------------------------------------------------------------------
# Pub/Sub push subscription → Cloud Run processor (POST /)
# ---------------------------------------------------------------------------

resource "google_pubsub_subscription" "push" {
  name  = "${var.pubsub_topic}-push"
  topic = google_pubsub_topic.hf_obs.id

  ack_deadline_seconds       = 30
  message_retention_duration = "600s"

  push_config {
    push_endpoint = "${google_cloud_run_v2_service.elim.uri}/"

    oidc_token {
      service_account_email = google_service_account.invoker.email
      audience              = google_cloud_run_v2_service.elim.uri
    }
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "60s"
  }

  depends_on = [google_cloud_run_v2_service_iam_member.invoker]
}

# ---------------------------------------------------------------------------
# Cloud Scheduler → POST /publish (every 2 min)
# ---------------------------------------------------------------------------

resource "google_cloud_scheduler_job" "publish" {
  name     = "${var.service_name}-publish"
  schedule = var.scheduler_cron
  region   = var.region

  description      = "Fetch HF weather observations and publish to Pub/Sub"
  time_zone        = "Etc/UTC"
  attempt_deadline = "${var.timeout_seconds}s"

  http_target {
    uri         = "${google_cloud_run_v2_service.elim.uri}/publish"
    http_method = "POST"

    oidc_token {
      service_account_email = google_service_account.invoker.email
      audience              = google_cloud_run_v2_service.elim.uri
    }
  }

  retry_config {
    retry_count          = 1
    min_backoff_duration = "5s"
    max_backoff_duration = "10s"
  }

  depends_on = [
    google_project_service.apis,
    google_cloud_run_v2_service_iam_member.invoker,
  ]
}
