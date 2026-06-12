"""Cloud Function (2nd gen): parse an order CSV from GCS and write one
Firestore document per row.

Triggered by google.cloud.storage.object.v1.finalized events on the
orders_processed bucket (see ../../gcp_function.tf) - the same objects the
worker (worker/src/uploader.py) writes, filtered here to orders/*.csv. Each
CSV row becomes one document at orders/{orderId}/lineItems/{sku}; a
redelivered event overwrites the same document with identical data (except
ingestedAt), so this handler is idempotent by construction - no separate
dedupe table needed.
"""

import csv
import io

import functions_framework
from cloudevents.http import CloudEvent
from google.cloud import firestore, storage

storage_client = storage.Client()
firestore_client = firestore.Client()


@functions_framework.cloud_event
def handle_gcs_event(cloud_event: CloudEvent) -> None:
    data = cloud_event.data
    bucket_name = data["bucket"]
    object_name = data["name"]

    if not (object_name.startswith("orders/") and object_name.endswith(".csv")):
        return

    content = storage_client.bucket(bucket_name).blob(object_name).download_as_text()

    for row in csv.DictReader(io.StringIO(content)):
        doc_ref = (
            firestore_client.collection("orders")
            .document(row["order_id"])
            .collection("lineItems")
            .document(row["sku"])
        )
        doc_ref.set(
            {
                "sku": row["sku"],
                "quantity": int(row["quantity"]),
                "channel": row["channel"],
                "sourceObjectName": object_name,
                "ingestedAt": firestore.SERVER_TIMESTAMP,
            }
        )
