# Runbook

## Starting / stopping / resetting the stack

```sh
make up             # build images and start everything (detached)
make ps             # check service status
make logs           # tail logs for all services
make down           # stop containers, keep volumes (data persists)
make down-v         # stop containers AND remove volumes (full reset)
```

`make smoke` brings the stack up, waits for every service to report healthy,
drops a synthetic order CSV onto the SFTP server, and confirms it lands in
both MinIO and fake-gcs. `make drop-file` does just the drop+verify step
against an already-running stack.

## Observability

### Health & metrics endpoints (worker, port 8080)

| Endpoint | Meaning |
|---|---|
| `GET /health` | Liveness - process is up. |
| `GET /ready` | Readiness - 200 only once both the watcher and consumer threads have a live RabbitMQ connection; 503 with a `components` breakdown otherwise. |
| `GET /metrics` | Prometheus text format - see `docs/architecture.md` for the metric list. |

```sh
curl -s localhost:8080/health
curl -s localhost:8080/ready
curl -s localhost:8080/metrics | grep -E 'events_|uploads_|dlq_|idempotency_'
```

### Logs

Every component logs single-line JSON to stdout
(`{"timestamp", "level", "service", "logger", "message", ...}`), with
`correlationId`, `eventId`, `idempotencyKey` and `attempt` included whenever
they're available - so a single file's journey can be grepped end-to-end:

```sh
docker compose -f infra/docker-compose.yml logs worker | grep '"correlationId":"<id>"'
```

### RabbitMQ management UI

`http://localhost:15672` (guest/guest, or `RABBITMQ_USER`/`RABBITMQ_PASS` if
overridden). Useful views:

- **Queues** tab - depth of `orders.inbound`, `orders.inbound.retry`, and
  `orders.inbound.dlq`. A growing `orders.inbound.retry` depth under sustained
  failure is expected (bounded by `RETRY_MAX_ATTEMPTS`); a non-zero
  `orders.inbound.dlq` depth needs investigation (see below).

## Common failure modes

### A bucket is missing / uploads fail with a "no such bucket" error
The buckets are created by the one-shot `minio-init` and `fake-gcs-init`
services. Check they ran successfully:

```sh
docker compose -f infra/docker-compose.yml ps minio-init fake-gcs-init
docker compose -f infra/docker-compose.yml logs minio-init fake-gcs-init
```

Both should show `Exited (0)`. Re-run with `docker compose -f
infra/docker-compose.yml up minio-init fake-gcs-init` if needed - both are
idempotent (`--ignore-existing` / `|| true`).

### Messages piling up in `orders.inbound.dlq`
Inspect via the management UI: **Queues → orders.inbound.dlq → Get
messages**. The message body's `retry.lastError` (or the `x-dlq-reason`
header) tells you why:

- `unsupported_schema_version` - the event's `schemaVersion` major doesn't
  match `SUPPORTED_SCHEMA_MAJOR` in `src/schema.py`. Indicates a
  producer/consumer version mismatch.
- `source_file_missing` - the file the event points to no longer exists on
  `/watch` (e.g. removed before the consumer ran).
- `max_retries_exceeded` - all `RETRY_MAX_ATTEMPTS` attempts failed; check
  `retry.lastError` for the underlying upload error
  (`<provider>:upload_timeout`, `<provider>:<ExceptionClassName>`, etc.) and
  the `worker` logs around that `correlationId`.
- `invalid_payload` - the message body wasn't valid JSON / didn't match the
  schema at all (raw passthrough, no `retry`/`x-dlq-reason`).

**Replay**: once the underlying issue is fixed, use the management UI's "Get
messages" with **Requeue: false** to copy the body, then **Publish message**
to `orders.inbound` (or use a short `pika` script) to reprocess it. The
consumer's idempotency check means a message that *did* partially succeed
before landing in the DLQ won't be double-uploaded.

### Duplicate events / "skipping duplicate event" in logs
Expected and harmless - it means `idempotencyKey` was already marked done in
the idempotency store (e.g. after a redelivery or a `make restart-worker`).
`idempotency_hits_total` increments each time. To inspect the store directly:

```sh
docker compose -f infra/docker-compose.yml exec worker \
  python -c "import sqlite3; c=sqlite3.connect('/data/idempotency.db'); \
  print(c.execute('SELECT * FROM processed_events').fetchall())"
```

(`sqlite3` the CLI binary isn't installed in the runtime image to keep it
minimal - the snippet above uses the stdlib `sqlite3` module instead.)

### RabbitMQ auth errors (`ACCESS_REFUSED`) from the worker
RabbitMQ restricts the default `guest` user to `localhost` connections; the
worker connects over the `internal` Docker network. This is already handled
by `loopback_users.guest = false` in `docker/rabbitmq/rabbitmq.conf` - if
you've changed `RABBITMQ_USER`/`RABBITMQ_PASS` away from `guest`, make sure
the new user has permissions on the vhost (`RABBITMQ_DEFAULT_VHOST`).

### Retry/backoff timing
`delay = RETRY_BACKOFF_BASE ** attempt` seconds (default base `2`): attempt 1
→ 2s, attempt 2 → 4s, attempt 3 → 8s. With the default `RETRY_MAX_ATTEMPTS=3`,
a message that keeps failing reaches the DLQ roughly 14s after its first
failure.

## Idempotency-on-restart demo

```sh
make drop-file                 # drop a file, wait for it to land in both clouds
make restart-worker             # restart the worker - SQLite DB persists in worker-data volume
make drop-file                 # drop another file - new files still process normally
```

To prove a *redelivery* of the same event is deduped (rather than just a new
file being processed normally), requeue a message from `orders.inbound.dlq`
or `orders.inbound.retry` for a file that was already fully processed via the
management UI, and watch the worker logs for `"duplicate event, skipping"`.
