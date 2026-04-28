variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region for all resources"
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Cloud Run service name"
  type        = string
  default     = "hf-elim-pubsub"
}

variable "pubsub_topic" {
  description = "Pub/Sub topic for HF weather observations"
  type        = string
  default     = "hf-weather-obs"
}

variable "scheduler_cron" {
  description = "Cloud Scheduler cron expression for publisher trigger"
  type        = string
  default     = "*/2 * * * *"
}

# ---------------------------------------------------------------------------
# Cloud Run container config
# ---------------------------------------------------------------------------

variable "image" {
  description = "Container image URL (gcr.io or artifact registry). Build separately via `gcloud builds submit`."
  type        = string
}

variable "memory" {
  description = "Cloud Run memory limit"
  type        = string
  default     = "512Mi"
}

variable "cpu" {
  description = "Cloud Run CPU limit"
  type        = string
  default     = "1"
}

variable "max_instances" {
  description = "Cloud Run max instances"
  type        = number
  default     = 3
}

variable "min_instances" {
  description = "Cloud Run min instances (0 = scale to zero)"
  type        = number
  default     = 0
}

variable "timeout_seconds" {
  description = "Cloud Run request timeout"
  type        = number
  default     = 120
}

# ---------------------------------------------------------------------------
# Secrets — references to GCP Secret Manager secrets (must exist already)
# ---------------------------------------------------------------------------

variable "telegram_bot_token_secret" {
  description = "Secret Manager secret name for TELEGRAM_BOT_TOKEN"
  type        = string
  default     = "TELEGRAM_BOT_TOKEN"
}

variable "postpeak_elim_chat_id_secret" {
  description = "Secret Manager secret name for POSTPEAK_ELIM_CHAT_ID"
  type        = string
  default     = "POSTPEAK_ELIM_CHAT_ID"
}

variable "anthropic_api_key_secret" {
  description = "Secret Manager secret name for ANTHROPIC_API_KEY (LLM gate)"
  type        = string
  default     = "ANTHROPIC_API_KEY"
}
