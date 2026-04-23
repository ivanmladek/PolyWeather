output "service_url" {
  description = "Cloud Run service URL"
  value       = google_cloud_run_v2_service.elim.uri
}

output "publish_endpoint" {
  description = "Publisher endpoint (Cloud Scheduler target)"
  value       = "${google_cloud_run_v2_service.elim.uri}/publish"
}

output "processor_endpoint" {
  description = "Processor endpoint (Pub/Sub push target)"
  value       = "${google_cloud_run_v2_service.elim.uri}/"
}

output "pubsub_topic" {
  description = "Pub/Sub topic name"
  value       = google_pubsub_topic.hf_obs.name
}

output "pubsub_subscription" {
  description = "Pub/Sub push subscription name"
  value       = google_pubsub_subscription.push.name
}

output "scheduler_job" {
  description = "Cloud Scheduler job name"
  value       = google_cloud_scheduler_job.publish.name
}

output "invoker_sa" {
  description = "Service account used by Scheduler & Pub/Sub to invoke Cloud Run"
  value       = google_service_account.invoker.email
}

output "runtime_sa" {
  description = "Service account used by the Cloud Run service (Secret Manager access)"
  value       = google_service_account.runtime.email
}
