#!/usr/bin/env bash
# Wait for every service in the local stack to report healthy (or, for
# one-shot init containers, to have exited successfully), then drop test
# file(s) through the pipeline via drop-test-files.py to verify it end to
# end. Used by `make smoke` and the CI e2e job.
#
# Set FILE_COUNT to drop more than one randomized file (see drop-test-files.py).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/infra/docker-compose.yml"
COMPOSE=(docker compose -f "$COMPOSE_FILE")

TIMEOUT="${TIMEOUT:-180}"
INTERVAL=2

# Services with a HEALTHCHECK that must report "healthy".
HEALTHY_SERVICES=(sftp rabbitmq minio fake-gcs worker)

# One-shot services that must exit 0.
ONESHOT_SERVICES=(minio-init fake-gcs-init)

elapsed=0
while true; do
  all_ok=true
  status_line=""

  for svc in "${HEALTHY_SERVICES[@]}"; do
    cid=$("${COMPOSE[@]}" ps -q "$svc")
    status="missing"
    if [ -n "$cid" ]; then
      status=$(docker inspect --format='{{.State.Health.Status}}' "$cid" 2>/dev/null || echo "unknown")
    fi
    status_line+="${svc}=${status} "
    [ "$status" = "healthy" ] || all_ok=false
  done

  for svc in "${ONESHOT_SERVICES[@]}"; do
    cid=$("${COMPOSE[@]}" ps -aq "$svc")
    status="missing"
    if [ -n "$cid" ]; then
      running=$(docker inspect --format='{{.State.Running}}' "$cid" 2>/dev/null || echo "true")
      exit_code=$(docker inspect --format='{{.State.ExitCode}}' "$cid" 2>/dev/null || echo "1")
      if [ "$running" = "true" ]; then
        status="running"
      elif [ "$exit_code" = "0" ]; then
        status="exited(0)"
      else
        status="exited(${exit_code})"
      fi
    fi
    status_line+="${svc}=${status} "
    [ "$status" = "exited(0)" ] || all_ok=false
  done

  if $all_ok; then
    echo "stack healthy: ${status_line}"
    exec "$ROOT_DIR/scripts/drop-test-files.py"
  fi

  if [ "$elapsed" -ge "$TIMEOUT" ]; then
    echo "timed out after ${TIMEOUT}s waiting for stack to become healthy" >&2
    echo "last status: ${status_line}" >&2
    "${COMPOSE[@]}" ps >&2
    "${COMPOSE[@]}" logs --tail=50 >&2
    exit 1
  fi

  sleep "$INTERVAL"
  elapsed=$((elapsed + INTERVAL))
done
