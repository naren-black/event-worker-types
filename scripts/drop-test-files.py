#!/usr/bin/env python3
"""Drop multiple synthetic order CSVs onto the SFTP server and wait for the
worker to copy each one to both MinIO (S3) and fake-gcs (GCS).

Like drop-test-file.sh, but generates FILE_COUNT files with randomized
order-line rows in one go (stdlib only - no extra dependencies).

Usage:
    ./scripts/drop-test-files.py
    FILE_COUNT=5 ./scripts/drop-test-files.py
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
COMPOSE_FILE = ROOT_DIR / "infra" / "docker-compose.yml"
COMPOSE = ["docker", "compose", "-f", str(COMPOSE_FILE)]

S3_BUCKET = os.environ.get("S3_BUCKET", "wms-orders-processed")
GCS_BUCKET = os.environ.get("GCS_BUCKET", "wms-orders-processed")
MINIO_ROOT_USER = os.environ.get("MINIO_ROOT_USER", "minioadmin")
MINIO_ROOT_PASSWORD = os.environ.get("MINIO_ROOT_PASSWORD", "minioadmin")
TIMEOUT = float(os.environ.get("TIMEOUT", "60"))
FILE_COUNT = int(os.environ.get("FILE_COUNT", "5"))
VERIFY = os.environ.get("VERIFY", "1") != "0"

CHANNELS = ["storefront", "ebay", "amazon"]


def build_csv(ts: str, idx: str) -> str:
    lines = ["order_id,sku,quantity,channel"]
    for line_no in range(1, random.randint(1, 5) + 1):
        sku = random.randint(1000, 9999)
        qty = random.randint(1, 10)
        channel = random.choice(CHANNELS)
        lines.append(f"ORD-{ts}-{idx}-{line_no},SKU-{sku},{qty},{channel}")
    return "\n".join(lines) + "\n"


def gcs_has(key: str) -> bool:
    url = f"http://localhost:4443/storage/v1/b/{GCS_BUCKET}/o/{urllib.parse.quote(key, safe='')}"
    try:
        urllib.request.urlopen(url, timeout=5)
        return True
    except (urllib.error.HTTPError, urllib.error.URLError):
        return False


def s3_has_all(keys: list[str]) -> bool:
    checks = " && ".join(f"mc stat 'local/{S3_BUCKET}/{key}' >/dev/null 2>&1" for key in keys)
    script = (
        f"mc alias set local http://minio:9000 '{MINIO_ROOT_USER}' '{MINIO_ROOT_PASSWORD}' >/dev/null 2>&1"
        f" && {checks}"
    )
    result = subprocess.run(
        [*COMPOSE, "run", "--rm", "--entrypoint", "sh", "minio-init", "-c", script],
        capture_output=True,
    )
    return result.returncode == 0


def main() -> int:
    ts = datetime.now().strftime("%Y%m%d%H%M%S")

    filenames = []
    tmp_paths = []
    for i in range(1, FILE_COUNT + 1):
        idx = f"{i:03d}"
        filename = f"ORD-{ts}-{idx}.csv"
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".csv") as f:
            f.write(build_csv(ts, idx))
            tmp_path = f.name
        os.chmod(tmp_path, 0o644)
        filenames.append(filename)
        tmp_paths.append(tmp_path)

    try:
        print(f"Dropping {FILE_COUNT} file(s) onto the SFTP server...", flush=True)
        for filename, tmp_path in zip(filenames, tmp_paths):
            subprocess.run(
                [*COMPOSE, "cp", tmp_path, f"sftp:/config/upload/{filename}"],
                check=True,
            )

        # docker compose cp lands as root; match SFTP-owned files so the
        # worker (uid 10001, supplementary gid 1001) can read the shared volume.
        subprocess.run(
            [
                *COMPOSE,
                "exec",
                "-T",
                "sftp",
                "sh",
                "-c",
                f"chown sftpuser:sftpuser /config/upload/ORD-{ts}-*.csv"
                f" && chmod 664 /config/upload/ORD-{ts}-*.csv",
            ],
            check=True,
        )
    finally:
        for tmp_path in tmp_paths:
            os.unlink(tmp_path)

    if not VERIFY:
        print(f"Dropped {FILE_COUNT} file(s) (VERIFY=0, skipping S3/GCS check):")
        for filename in filenames:
            print(f"  {filename}")
        return 0

    # Objects land at the bare filename: WATCH_DIR=/watch *is* the SFTP
    # upload root (sftp-data volume), so relative_key has no "orders/" prefix.
    keys = filenames

    print(
        f"Waiting up to {TIMEOUT:.0f}s for the worker to copy {FILE_COUNT} file(s) "
        "to S3 (MinIO) and GCS (fake-gcs)..."
    )

    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        if all(gcs_has(key) for key in keys) and s3_has_all(keys):
            print(f"OK: all {FILE_COUNT} file(s) present in s3://{S3_BUCKET}/ and gs://{GCS_BUCKET}/:")
            for filename in filenames:
                print(f"  {filename}")
            return 0
        time.sleep(2)

    print(f"Timed out after {TIMEOUT:.0f}s waiting for {FILE_COUNT} file(s) to land in both destinations", file=sys.stderr)
    print("--- worker logs (tail) ---", file=sys.stderr)
    subprocess.run([*COMPOSE, "logs", "--tail=50", "worker"])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
