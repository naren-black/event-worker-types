# Architecture

## Pipeline overview

```mermaid
flowchart LR
    subgraph sftp["SFTP (atmoz/sftp)"]
        F[/Dropped CSV file/]
    end

    subgraph worker["worker process (python -m src.main)"]
        direction TB
        W["Watcher thread\n(inotify via watchdog)"]
        C["Consumer thread\n(validate -> dedupe -> upload -> ack/retry/DLQ)"]
        I[("Idempotency store\nSQLite, /data/idempotency.db")]
        H["Health/metrics server\n:8080 - /health /ready /metrics"]
        C <--> I
    end

    subgraph rabbitmq["RabbitMQ"]
        Q["orders.inbound\n(main queue)"]
        R["orders.inbound.retry\n(per-message TTL)"]
        D["orders.inbound.dlq"]
    end

    subgraph clouds["Object storage"]
        S3[("MinIO / S3\nwms-orders-processed")]
        GCS[("fake-gcs / GCS\nwms-orders-processed")]
    end

    F -- "IN_CLOSE_WRITE / moved" --> W
    W -- "publish file.transfer.requested" --> Q
    Q --> C
    C -- "concurrent upload" --> S3
    C -- "concurrent upload" --> GCS
    C -- "transient failure\n(publish with TTL)" --> R
    R -- "TTL expiry\n(dead-letter)" --> Q
    C -- "permanent failure /\nmax retries exceeded" --> D
```

## Components

### Watcher thread (`src/watcher.py`)
Uses [`watchdog`](https://pypi.org/project/watchdog/) (inotify on Linux) to watch
`WATCH_DIR` recursively. `IN_CLOSE_WRITE` (a write completed) and move/rename
events (common for SFTP clients that write to a temp name and rename into
place) both trigger `build_event()`, which:

- computes a SHA-256 checksum and size for the file,
- derives a stable `idempotencyKey` (HMAC-SHA256 over path + size + checksum,
  see `src/utils.py:compute_idempotency_key`),
- builds a `file.transfer.requested` event (`src/schema.py:TransferEvent`)
  with one `destination` per cloud target (`aws-s3`, `gcp-gcs`),
- publishes it to `orders.inbound` via `src/publisher.py:Publisher`.

The watchdog `Observer` runs callbacks on its own thread, but
`pika.BlockingConnection` is not thread-safe. The event handler only pushes
detected paths onto a `queue.Queue`; `run_watcher` (on the watcher thread)
drains that queue and does the actual publish, so all broker I/O for this
connection stays single-threaded.

### RabbitMQ topology (`src/publisher.py:declare_topology`)
Declared idempotently at startup by **both** the watcher and consumer
connections (RabbitMQ no-ops a `queue_declare` with identical arguments), so
it's the single source of truth - there is no separate `definitions.json` to
keep in sync.

| Queue | Purpose |
|---|---|
| `orders.inbound` | Main queue, consumed by `src.consumer`. |
| `orders.inbound.retry` | Holding queue. Messages are published here with a per-message TTL (the `expiration` property); `x-dead-letter-exchange`/`x-dead-letter-routing-key` send them back to `orders.inbound` on expiry - i.e. delayed retry without the delayed-message-exchange plugin. |
| `orders.inbound.dlq` | Terminal failures - inspected/replayed manually (see `docs/runbook.md`). |

### Consumer thread (`src/consumer.py`)
Drives a validate → dedupe → upload → ack/retry/DLQ state machine, one
message at a time (`prefetch_count = UPLOAD_MAX_CONCURRENCY`):

1. **Parse & validate** the event against the schema (`src/schema.py`). An
   invalid payload or unsupported `schemaVersion` major goes straight to the
   DLQ - retrying a malformed message can't fix it.
2. **Idempotency check** (`src/idempotency.py`): if `idempotencyKey` is
   already marked done, ack and skip - this makes redeliveries (broker
   crash, requeue after worker restart) safe.
3. **Upload** the source file to every `destination` concurrently
   (`src/uploader.py:upload_all`).
4. **On full success**: mark the idempotency key done, ack.
5. **On any failure**: if `retry.attempt` would exceed `RETRY_MAX_ATTEMPTS`,
   publish to the DLQ with `retry.lastError` populated; otherwise publish to
   `orders.inbound.retry` with `expiration = RETRY_BACKOFF_BASE ** attempt`
   seconds and ack the original message.

### Uploader (`src/uploader.py`)
Both `boto3` (S3/MinIO) and `google-cloud-storage` (GCS/fake-gcs) are
synchronous, so each destination's upload runs via `asyncio.to_thread`,
bounded by an `asyncio.Semaphore(UPLOAD_MAX_CONCURRENCY)` and a per-destination
`asyncio.wait_for(..., timeout=UPLOAD_TIMEOUT_S)`. Every upload returns an
`UploadResult` (never raises) so the consumer can decide retry vs. DLQ for
each destination independently while still waiting for *all* of them.

### Idempotency store (`src/idempotency.py`)
A small SQLite database (`IDEMPOTENCY_DB_PATH`, default `/data/idempotency.db`,
backed by the `worker-data` volume in `infra/docker-compose.yml`) recording
`idempotency_key -> correlation_id` for fully-processed events. WAL mode lets
the watcher and consumer threads share one connection safely. Surviving
`docker compose restart worker` is the point - see `docs/runbook.md` for the
restart demo.

### Health/metrics server (`src/health.py`)
A dependency-free `ThreadingHTTPServer` on the main thread, serving:

- `/health` - liveness (process is up).
- `/ready` - readiness; 200 only once **both** the watcher and consumer
  threads report a live broker connection (`ReadinessState`).
- `/metrics` - Prometheus text format (`src/metrics.py`):
  `events_published_total`, `events_consumed_total{result}`,
  `upload_duration_seconds{provider}`, `uploads_total{provider,result}`,
  `dlq_messages_total`, `idempotency_hits_total`.

## Data-flow walkthroughs

**Happy path**: file lands → watcher detects `IN_CLOSE_WRITE` → publishes to
`orders.inbound` → consumer validates, dedupe-checks (miss), uploads to MinIO
+ fake-gcs concurrently, both succeed → idempotency key marked done → message
acked. `events_consumed_total{result="success"}` increments.

**Retry path**: one or both uploads fail transiently (e.g. timeout) →
consumer computes `attempt = previous_attempt + 1`, publishes the event to
`orders.inbound.retry` with `expiration = (RETRY_BACKOFF_BASE ** attempt) * 1000`
ms, acks the original message → after the TTL expires, RabbitMQ dead-letters
the message back onto `orders.inbound` → consumer processes it again with
`retry.attempt` incremented. `events_consumed_total{result="retry"}`
increments each time.

**DLQ path**: either (a) the event fails schema validation/version check, (b)
the source file is missing on disk, or (c) `attempt > RETRY_MAX_ATTEMPTS` -
in all cases the consumer publishes to `orders.inbound.dlq` (with
`x-dlq-reason` / `retry.lastError` context) and acks the original message, so
it's never silently dropped or stuck retrying forever.
`dlq_messages_total` increments.

## Design rationale

- **Broker-first.** The watcher only has to durably publish one small JSON
  event; everything slow/fallible (uploads to two clouds) happens on the
  consumer side, decoupled by RabbitMQ. A worker crash mid-upload just means
  the message is redelivered - no bespoke crash-recovery logic needed.
- **TTL + dead-letter-exchange retry instead of the delayed-message plugin.**
  `rabbitmq:3.13-management` doesn't ship the
  `rabbitmq_delayed_message_exchange` community plugin. A holding queue with
  per-message `expiration` and `x-dead-letter-*` arguments gives the same
  delayed-retry behaviour using only stock RabbitMQ features.
- **HMAC-derived idempotency key + SQLite.** Deriving the key from
  `path|size|checksum` under a shared secret makes it stable across
  redeliveries (so dedupe works) while not being guessable/forgeable by
  anything that doesn't hold the secret. SQLite is enough for a
  single-instance worker and needs zero extra infrastructure; a multi-instance
  deployment would swap this for Redis/Postgres (see `ADR.md`).
- **Concurrent dual-cloud upload via asyncio + semaphore + per-destination
  timeout.** Both cloud SDKs are synchronous; `asyncio.to_thread` lets both
  uploads run in parallel without a second process, while the semaphore caps
  concurrent threads and the per-destination timeout stops one slow provider
  from blocking the other's result.
- **Stdlib-only health/metrics server.** No Flask/FastAPI dependency keeps the
  runtime image smaller and reduces the surface `pip-audit`/`bandit` need to
  cover, for three small endpoints.
