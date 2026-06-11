# ADR 0001: Broker-first, event-driven cross-cloud transfer worker

**Status:** Accepted

## Context

Exercise 2 calls for a worker that reacts to a file landing on SFTP and
reliably copies it to two cloud object stores (AWS S3 and GCP GCS), with:

- event-driven triggering (no polling for new files),
- idempotency (a redelivered or duplicate event must not double-upload),
- retries with backoff and a dead-letter queue for terminal failures,
- both cloud uploads attempted concurrently per file,
- unit tests, containerization (multi-stage, non-root, `HEALTHCHECK`), CI
  (build, test, security scan), structured logs/metrics, and local emulators
  (MinIO, fake-gcs-server) so the whole thing runs with `docker compose up`
  and no real cloud accounts.

## Decision

**Broker-first**: an inotify-based watcher publishes a small JSON
`file.transfer.requested` event (contract: `docs/event-contract.md`,
`schemas/transfer-event.schema.json`) to RabbitMQ; a separate consumer does
the actual work. Specific choices:

- **TTL + dead-letter-exchange retry**, not the delayed-message-exchange
  plugin. A holding queue (`orders.inbound.retry`) with a per-message
  `expiration` and `x-dead-letter-routing-key` back to the main queue gives
  delayed redelivery using only stock `rabbitmq:3.13-management` - no
  non-default plugin to install/maintain.
- **Idempotency via an HMAC-derived key + SQLite.** `idempotencyKey =
  sha256(HMAC(secret, path|size|checksum))` is stable across redeliveries (so
  it works as a dedupe key) but not forgeable without the worker's secret. A
  local SQLite DB (WAL mode, on a named volume) records completed keys -
  enough for a single-instance worker, with zero extra infrastructure.
- **Concurrent uploads via `asyncio` + a semaphore + per-destination
  timeout.** `boto3` and `google-cloud-storage` are both synchronous; running
  each destination's upload in `asyncio.to_thread` lets both clouds be
  attempted in parallel from one process, bounded by
  `UPLOAD_MAX_CONCURRENCY` and `UPLOAD_TIMEOUT_S` per destination.
- **A dependency-free `ThreadingHTTPServer`** for `/health`, `/ready`, and a
  hand-rolled Prometheus `/metrics` exporter (`prometheus-client`), instead of
  a web framework.

## Alternatives considered & rejected

- **Direct upload from the watcher** (no broker). Simplest, but a crash
  mid-upload loses the event entirely - no durability, no retry, no DLQ. The
  brief explicitly asks for retry/DLQ and resilience to failures, which needs
  a durable intermediary.
- **RabbitMQ delayed-message-exchange plugin** for retry backoff. Cleaner API
  (`x-delay` header) but it's a community plugin not present in the stock
  management image - extra image-build complexity for a local demo, and an
  operational dependency a real deployment would need to track separately.
  TTL+DLX achieves the same delayed-retry semantics with zero extra plugins.
- **Celery or another task-queue framework.** Would add a large dependency
  surface (and its own broker assumptions) for what is, at its core, a single
  consume-process-ack loop that RabbitMQ's native client already supports
  directly.
- **Flask/FastAPI for the health endpoint.** Three endpoints
  (`/health`, `/ready`, `/metrics`) don't justify a web framework dependency -
  every extra dependency is something `pip-audit`/`bandit` and the image's
  attack surface have to account for.

## Consequences / trade-offs

- **Two `pika` connections in one process** (watcher and consumer each open
  their own `BlockingConnection`, since it isn't thread-safe). This adds a
  little internal complexity versus splitting them into separate
  services/containers, but keeps the demo to a single image and process tree.
- **SQLite idempotency store is single-instance.** It lives on a Docker
  volume tied to one `worker` container. Running multiple worker replicas for
  throughput would need a shared store (Redis or Postgres) instead - the
  `IdempotencyStore` interface (`is_done` / `mark_done`) is small enough to
  swap the backend without touching the consumer logic.
- **`orders.inbound.retry` queue depth grows under sustained failure**, bounded
  by `RETRY_MAX_ATTEMPTS` per message before it moves to the DLQ - by design,
  but worth alerting on in a real deployment (see `docs/runbook.md`).
- **Local emulators (MinIO, fake-gcs-server) stand in for real AWS/GCP.** The
  identity/auth posture for a real deployment (IRSA / Workload Identity
  Federation, least-privilege per bucket) is documented in
  `docs/event-contract.md` and sketched (illustratively, non-deployable) in
  `infra/terraform/`.
