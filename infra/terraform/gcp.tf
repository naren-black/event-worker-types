# Illustrative only - see README.md.
# Mirrors the bucket created locally by infra/docker-compose.yml's
# `fake-gcs-init` service, plus the Workload Identity Federation posture
# described in docs/event-contract.md (section 3).

resource "google_storage_bucket" "orders_processed" {
  name                        = var.bucket_name
  project                     = var.gcp_project_id
  location                    = "US"
  uniform_bucket_level_access = true

  versioning {
    enabled = true
  }
}

# The worker authenticates as this identity via Workload Identity Federation -
# no downloadable JSON key is ever created.
resource "google_service_account" "worker" {
  project      = var.gcp_project_id
  account_id   = "sftp-event-worker"
  display_name = "sftp-event-worker"
}

# Least privilege: object read/write on this bucket only, not project-wide
# storage admin.
resource "google_storage_bucket_iam_member" "worker_object_admin" {
  bucket = google_storage_bucket.orders_processed.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.worker.email}"
}

# Bind the external workload identity (the worker's federated identity) to
# impersonate the service account above.
resource "google_service_account_iam_member" "worker_workload_identity" {
  service_account_id = google_service_account.worker.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${var.gcp_workload_identity_pool}/*"
}
