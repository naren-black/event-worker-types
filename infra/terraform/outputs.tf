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

output "aws_order_csv_to_dynamodb_function_name" {
  description = "Lambda function that parses order CSVs from S3 into DynamoDB."
  value       = aws_lambda_function.order_csv_to_dynamodb.function_name
}

output "aws_order_line_items_table_name" {
  description = "DynamoDB table holding parsed order CSV rows (PK orderId, SK sku)."
  value       = aws_dynamodb_table.order_line_items.name
}

output "aws_order_csv_to_dynamodb_dlq_url" {
  description = "SQS queue receiving failed order-csv-to-dynamodb invocations."
  value       = aws_sqs_queue.order_csv_to_dynamodb_dlq.url
}

output "gcp_order_csv_to_firestore_function_name" {
  description = "Cloud Function that parses order CSVs from GCS into Firestore."
  value       = google_cloudfunctions2_function.order_csv_to_firestore.name
}

output "gcp_order_csv_to_firestore_uri" {
  description = "Underlying Cloud Run URI for the order-csv-to-firestore function (useful for manual invocation/debugging)."
  value       = google_cloudfunctions2_function.order_csv_to_firestore.service_config[0].uri
}

output "gcp_firestore_database_name" {
  description = "Firestore (Native mode) database holding parsed order CSV rows under orders/{orderId}/lineItems/{sku}."
  value       = google_firestore_database.default.name
}
