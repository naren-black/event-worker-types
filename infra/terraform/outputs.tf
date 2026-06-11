# Illustrative only - see README.md.

output "aws_worker_role_arn" {
  description = "IAM role the worker assumes via IRSA for S3 access."
  value       = aws_iam_role.worker.arn
}

output "aws_bucket_name" {
  description = "S3 bucket for processed order files."
  value       = aws_s3_bucket.orders_processed.bucket
}

output "gcp_worker_service_account_email" {
  description = "Service account the worker impersonates via Workload Identity Federation for GCS access."
  value       = google_service_account.worker.email
}

output "gcp_bucket_name" {
  description = "GCS bucket for processed order files."
  value       = google_storage_bucket.orders_processed.name
}
