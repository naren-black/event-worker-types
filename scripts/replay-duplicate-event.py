#!/usr/bin/env python3
"""Demonstrate idempotency_hits_total / events_consumed_total{result="duplicate"}.

The idempotency check in src/consumer.py runs BEFORE the source-file check,
so a "duplicate" message doesn't need its source file to exist on disk - it
just needs an idempotencyKey that the worker has already marked done.

Steps:
  1. Build one CSV with known content (size_bytes/checksum computed locally).
  2. Drop it onto the SFTP server and wait for the worker to process it once
     (confirmed via the object landing in fake-gcs - this also means
     IdempotencyStore.mark_done(idempotencyKey) was called).
  3. Recompute that file's idempotencyKey locally with the same HMAC as
     watcher.build_event() (requires CORRELATION_SECRET to match the worker's).
  4. Publish a fresh TransferEvent (new eventId/correlationId, same
     idempotencyKey) directly onto orders.inbound via the RabbitMQ management
     HTTP API - bypassing the watcher entirely.
  5. Poll /metrics until idempotency_hits_total and
     events_consumed_total{result="duplicate"} increment by 1, then show the
     worker's "duplicate event, skipping" log line.

Usage:
    ./scripts/replay-duplicate-event.py
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
COMPOSE_FILE = ROOT_DIR / "infra" / "docker-compose.yml"
COMPOSE = ["docker", "compose", "-f", str(COMPOSE_FILE)]

S3_BUCKET = os.environ.get("S3_BUCKET", "wms-orders-processed")
S3_REGION = os.environ.get("S3_REGION", "us-east-1")
GCS_BUCKET = os.environ.get("GCS_BUCKET", "wms-orders-processed")

RABBITMQ_USER = os.environ.get("RABBITMQ_USER", "guest")
RABBITMQ_PASS = os.environ.get("RABBITMQ_PASS", "guest")
RABBITMQ_VHOST = os.environ.get("RABBITMQ_VHOST", "/")
QUEUE_NAME = os.environ.get("QUEUE_NAME", "orders.inbound")

CORRELATION_SECRET = os.environ.get("CORRELATION_SECRET", "dev-secret-change-me")
WATCH_DIR = os.environ.get("WATCH_DIR", "/watch")

TIMEOUT = float(os.environ.get("TIMEOUT", "60"))
METRICS_URL = "http://localhost:8080/metrics"


def gcs_has(key: str) -> bool:
    url = f"http://localhost:4443/storage/v1/b/{GCS_BUCKET}/o/{urllib.parse.quote(key, safe='')}"
    try:
        urllib.request.urlopen(url, timeout=5)
        return True
    except (urllib.error.HTTPError, urllib.error.URLError):
        return False


def compute_idempotency_key(secret: str, source_path: str, size_bytes: int, checksum_sha256: str) -> str:
    message = f"{source_path}|{size_bytes}|{checksum_sha256}".encode()
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"sha256:{digest}"


def get_metric(name: str, label: str | None = None) -> float:
    """Read a single metric value from /metrics, defaulting to 0.0 if absent.

    Labeled counters (e.g. events_consumed_total{result="duplicate"}) don't
    appear in the output until .labels(...) has been called at least once.
    """
    try:
        with urllib.request.urlopen(METRICS_URL, timeout=5) as resp:
            text = resp.read().decode()
    except urllib.error.URLError:
        return 0.0

    if label is None:
        pattern = re.compile(rf"^{re.escape(name)} ([0-9.e+-]+)$", re.MULTILINE)
    else:
        pattern = re.compile(rf'^{re.escape(name)}\{{{re.escape(label)}\}} ([0-9.e+-]+)$', re.MULTILINE)

    match = pattern.search(text)
    return float(match.group(1)) if match else 0.0


def rabbitmq_publish(payload_json: str) -> bool:
    vhost = urllib.parse.quote(RABBITMQ_VHOST, safe="")
    url = f"http://localhost:15672/api/exchanges/{vhost}/amq.default/publish"
    body = json.dumps(
        {
            "properties": {"content_type": "application/json", "delivery_mode": 2},
            "routing_key": QUEUE_NAME,
            "payload": payload_json,
            "payload_encoding": "string",
        }
    ).encode()

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    creds = base64.b64encode(f"{RABBITMQ_USER}:{RABBITMQ_PASS}".encode()).decode()
    req.add_header("Authorization", f"Basic {creds}")

    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
    return bool(result.get("routed"))


def main() -> int:
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    filename = f"ORD-{ts}-dup.csv"
    content = (
        "order_id,sku,quantity,channel\n"
        f"ORD-{ts}-dup-1,SKU-1234,3,storefront\n"
    )
    content_bytes = content.encode()
    size_bytes = len(content_bytes)
    checksum = hashlib.sha256(content_bytes).hexdigest()

    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".csv") as f:
        f.write(content)
        tmp_path = f.name
    os.chmod(tmp_path, 0o644)

    try:
        print(f"Dropping {filename} onto the SFTP server...", flush=True)
        subprocess.run([*COMPOSE, "cp", tmp_path, f"sftp:/config/upload/{filename}"], check=True)
        subprocess.run(
            [
                *COMPOSE,
                "exec",
                "-T",
                "sftp",
                "sh",
                "-c",
                f"chown sftpuser:sftpuser /config/upload/{filename}"
                f" && chmod 664 /config/upload/{filename}",
            ],
            check=True,
        )
    finally:
        os.unlink(tmp_path)

    print(f"Waiting up to {TIMEOUT:.0f}s for the worker to process {filename} for the first time...")
    deadline = time.monotonic() + TIMEOUT
    while not gcs_has(filename):
        if time.monotonic() >= deadline:
            print(f"Timed out waiting for {filename} to land in gs://{GCS_BUCKET}/", file=sys.stderr)
            subprocess.run([*COMPOSE, "logs", "--tail=50", "worker"])
            return 1
        time.sleep(2)
    print(f"OK: {filename} processed once (present in gs://{GCS_BUCKET}/{filename}).\n")

    source_path = f"{WATCH_DIR}/{filename}"
    idempotency_key = compute_idempotency_key(CORRELATION_SECRET, source_path, size_bytes, checksum)
    print(f"Recomputed idempotencyKey: {idempotency_key}")

    event = {
        "schemaVersion": "1.0",
        "eventType": "file.transfer.requested",
        "eventId": str(uuid.uuid4()),
        "correlationId": str(uuid.uuid4()),
        "idempotencyKey": idempotency_key,
        "occurredAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {"provider": "sftp", "path": source_path},
        "file": {
            "name": filename,
            "sizeBytes": size_bytes,
            "checksumSha256": checksum,
            "contentType": "text/csv",
        },
        "destinations": [
            {"provider": "aws-s3", "bucket": S3_BUCKET, "key": filename, "region": S3_REGION},
            {"provider": "gcp-gcs", "bucket": GCS_BUCKET, "key": filename},
        ],
    }

    hits_before = get_metric("idempotency_hits_total")
    dup_before = get_metric("events_consumed_total", 'result="duplicate"')
    print(f"\nBefore replay: idempotency_hits_total={hits_before} events_consumed_total{{result=\"duplicate\"}}={dup_before}")

    print(f"\nPublishing replayed event (eventId={event['eventId']}) onto {QUEUE_NAME} via RabbitMQ management API...")
    routed = rabbitmq_publish(json.dumps(event))
    if not routed:
        print("RabbitMQ reported the message was NOT routed (queue missing?)", file=sys.stderr)
        return 1
    print("Published.")

    print(f"\nWaiting up to 20s for idempotency_hits_total / events_consumed_total{{result=\"duplicate\"}} to increment...")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        hits_after = get_metric("idempotency_hits_total")
        dup_after = get_metric("events_consumed_total", 'result="duplicate"')
        if hits_after > hits_before and dup_after > dup_before:
            print(f"\nOK: idempotency_hits_total {hits_before} -> {hits_after}")
            print(f"OK: events_consumed_total{{result=\"duplicate\"}} {dup_before} -> {dup_after}")
            print("\n--- worker logs (tail) ---")
            subprocess.run([*COMPOSE, "logs", "--tail=20", "worker"])
            return 0
        time.sleep(1)

    print("Timed out waiting for the duplicate-result metrics to increment", file=sys.stderr)
    subprocess.run([*COMPOSE, "logs", "--tail=50", "worker"])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
