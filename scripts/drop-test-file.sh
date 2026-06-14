#!/usr/bin/env bash
# Drop a synthetic order CSV onto the SFTP server (bypassing SFTP/SSH auth via
# `docker compose cp`, which triggers the same inotify event a real upload
# would) and wait for the worker to copy it to both MinIO (S3) and fake-gcs
# (GCS). Used by `make drop-file` / `make smoke` and the CI e2e job.
#
# For dropping multiple randomized files at once, see drop-test-files.py.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/infra/docker-compose.yml"
COMPOSE=(docker compose -f "$COMPOSE_FILE")

S3_BUCKET="${S3_BUCKET:-wms-orders-processed}"
GCS_BUCKET="${GCS_BUCKET:-wms-orders-processed}"
MINIO_ROOT_USER="${MINIO_ROOT_USER:-minioadmin}"
MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD:-minioadmin}"
TIMEOUT="${TIMEOUT:-60}"

ts=$(date +%Y%m%d%H%M%S)
filename="ORD-${ts}.csv"
# WATCH_DIR=/watch *is* the SFTP upload root (sftp-data volume), so the
# worker's relative_key has no "orders/" prefix.
key="${filename}"

tmpfile=$(mktemp)
trap 'rm -f "$tmpfile"' EXIT

cat > "$tmpfile" <<EOF
order_id,sku,quantity,channel
ORD-${ts}-1,SKU-1001,2,storefront
ORD-${ts}-2,SKU-2002,1,ebay
EOF
chmod 644 "$tmpfile"

echo "Dropping ${filename} onto the SFTP server..."
"${COMPOSE[@]}" cp "$tmpfile" "sftp:/config/upload/${filename}"
# docker compose cp lands as root; match SFTP-owned files so the worker (uid
# 10001, supplementary gid 1001) can read the shared volume.
"${COMPOSE[@]}" exec -T sftp chown sftpuser:sftpuser "/config/upload/${filename}"
"${COMPOSE[@]}" exec -T sftp chmod 664 "/config/upload/${filename}"

echo "Waiting up to ${TIMEOUT}s for the worker to copy ${key} to S3 (MinIO) and GCS (fake-gcs)..."
encoded_key=$(printf '%s' "$key" | sed 's#/#%2F#g')

elapsed=0
while [ "$elapsed" -lt "$TIMEOUT" ]; do
  gcs_ok=false
  if curl -sf "http://localhost:4443/storage/v1/b/${GCS_BUCKET}/o/${encoded_key}" >/dev/null 2>&1; then
    gcs_ok=true
  fi

  s3_ok=false
  if $gcs_ok; then
    result=$("${COMPOSE[@]}" run --rm --entrypoint sh minio-init -c \
      "mc alias set local http://minio:9000 '${MINIO_ROOT_USER}' '${MINIO_ROOT_PASSWORD}' >/dev/null 2>&1 && mc stat 'local/${S3_BUCKET}/${key}' >/dev/null 2>&1 && echo yes || echo no" \
      2>/dev/null | tr -d '\r\n')
    [ "$result" = "yes" ] && s3_ok=true
  fi

  if $gcs_ok && $s3_ok; then
    echo "OK: ${key} present in s3://${S3_BUCKET}/${key} and gs://${GCS_BUCKET}/${key}"
    echo "$filename"
    exit 0
  fi

  sleep 2
  elapsed=$((elapsed + 2))
done

echo "Timed out after ${TIMEOUT}s waiting for ${key} to land in both destinations" >&2
echo "--- worker logs (tail) ---" >&2
"${COMPOSE[@]}" logs --tail=50 worker >&2
exit 1
