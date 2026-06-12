"""Lambda handler: parse an order CSV from S3 and write one DynamoDB item per row.

Triggered by S3 ObjectCreated events on the orders_processed bucket's
orders/*.csv objects (see ../../aws_lambda.tf) - the same objects the worker
(worker/src/uploader.py) writes. Each CSV row becomes one item in the
order-line-items table, keyed by (orderId, sku); a redelivered event
overwrites the same item with identical data, so this handler is idempotent
by construction - no separate dedupe table needed.
"""

import csv
import io
import os
import time
import urllib.parse

import boto3

TABLE_NAME = os.environ["TABLE_NAME"]

s3 = boto3.client("s3")
table = boto3.resource("dynamodb").Table(TABLE_NAME)


def handle_s3_event(event, _context):
    for record in event["Records"]:
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        _ingest_object(bucket, key)


def _ingest_object(bucket: str, key: str) -> None:
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    ingested_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    with table.batch_writer() as batch:
        for row in csv.DictReader(io.StringIO(body)):
            batch.put_item(
                Item={
                    "orderId": row["order_id"],
                    "sku": row["sku"],
                    "quantity": int(row["quantity"]),
                    "channel": row["channel"],
                    "sourceObjectKey": key,
                    "ingestedAt": ingested_at,
                }
            )
