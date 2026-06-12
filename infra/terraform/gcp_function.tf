# Illustrative only - see README.md.
#
# Reacts to new CSVs landing under orders/ in the bucket from gcp.tf (the
# same objects worker/src/uploader.py writes), parses each row, and writes
# one document per row into Firestore. See ADR.md (ADR 0002) for the design
# rationale and alternatives considered.

resource "google_firestore_database" "default" {
  project     = var.gcp_project_id
  name        = "(default)"
  location_id = var.gcp_region
  type        = "FIRESTORE_NATIVE"
}

data "archive_file" "order_csv_to_firestore" {
  type        = "zip"
  source_dir  = "${path.module}/functions/gcp_order_csv_to_firestore"
  output_path = "${path.module}/.build/order_csv_to_firestore.zip"
}

# Cloud Functions (2nd gen) deploys from a source archive in GCS, not from a
# local path directly.
resource "google_storage_bucket" "function_source" {
  name                        = "${var.gcp_project_id}-function-source"
  project                     = var.gcp_project_id
  location                    = var.gcp_region
  uniform_bucket_level_access = true
}

resource "google_storage_bucket_object" "order_csv_to_firestore_source" {
  name   = "order-csv-to-firestore-${data.archive_file.order_csv_to_firestore.output_md5}.zip"
  bucket = google_storage_bucket.function_source.name
  source = data.archive_file.order_csv_to_firestore.output_path
}

resource "google_service_account" "order_csv_to_firestore" {
  project      = var.gcp_project_id
  account_id   = "order-csv-to-firestore"
  display_name = "order-csv-to-firestore Cloud Function"
}

# Least privilege: read-only on the source bucket, not storage.objectAdmin.
resource "google_storage_bucket_iam_member" "order_csv_to_firestore_read" {
  bucket = google_storage_bucket.orders_processed.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.order_csv_to_firestore.email}"
}

# Firestore Native mode is exposed via the Datastore API/IAM surface.
resource "google_project_iam_member" "order_csv_to_firestore_datastore" {
  project = var.gcp_project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.order_csv_to_firestore.email}"
}

# Eventarc's GCS trigger is mediated by Pub/Sub under the hood: the Cloud
# Storage service agent must be able to publish to the project's Pub/Sub
# topics, or the trigger never receives object-finalize notifications.
data "google_storage_project_service_account" "gcs_account" {
  project = var.gcp_project_id
}

resource "google_project_iam_member" "gcs_pubsub_publisher" {
  project = var.gcp_project_id
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${data.google_storage_project_service_account.gcs_account.email_address}"
}

resource "google_cloudfunctions2_function" "order_csv_to_firestore" {
  name     = "order-csv-to-firestore"
  project  = var.gcp_project_id
  location = var.gcp_region

  build_config {
    runtime     = "python312"
    entry_point = "handle_gcs_event"

    source {
      storage_source {
        bucket = google_storage_bucket.function_source.name
        object = google_storage_bucket_object.order_csv_to_firestore_source.name
      }
    }
  }

  service_config {
    available_memory      = "256M"
    timeout_seconds       = 60
    service_account_email = google_service_account.order_csv_to_firestore.email
  }

  event_trigger {
    trigger_region        = var.gcp_region
    event_type            = "google.cloud.storage.object.v1.finalized"
    retry_policy          = "RETRY_POLICY_RETRY"
    service_account_email = google_service_account.order_csv_to_firestore.email

    event_filters {
      attribute = "bucket"
      value     = google_storage_bucket.orders_processed.name
    }
  }

  depends_on = [google_project_iam_member.gcs_pubsub_publisher]
}
