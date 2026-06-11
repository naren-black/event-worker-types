# Event & Ops Contract — Cross-Cloud Transfer Worker

This is the contract Task 1 of Exercise 2 asks for: a compact event schema plus the
operational rules around it (failure handling, versioning, identity, run profile, SLOs).

## 1. Event schema

Machine-readable schema: [`schemas/transfer-event.schema.json`](../schemas/transfer-event.schema.json)
(JSON Schema draft 2020-12). Examples:

- [`schemas/example-event.json`](../schemas/example-event.json) — first publish
- [`schemas/example-event-retry.json`](../schemas/example-event-retry.json) — redelivery with `retry` populated

```json
{
  "schemaVersion": "1.0",
  "eventType": "file.transfer.requested",
  "eventId": "8f14e45f-ceea-4c4f-9f2a-1d2b3c4d5e6f",
  "correlationId": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "idempotencyKey": "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
  "occurredAt": "2026-06-10T09:32:11Z",
  "source": { "provider": "sftp", "path": "/watch/orders/ORD-20260610-0001.csv" },
  "file": {
    "name": "ORD-20260610-0001.csv",
    "sizeBytes": 4096,
    "checksumSha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "contentType": "text/csv"
  },
  "destinations": [
    { "provider": "aws-s3", "bucket": "wms-orders-processed", "key": "orders/ORD-20260610-0001.csv", "region": "us-east-1" },
    { "provider": "gcp-gcs", "bucket": "wms-orders-processed", "key": "orders/ORD-20260610-0001.csv" }
  ]
}
```

| Field | Purpose |
|---|---|
| `schemaVersion` | `MAJOR.MINOR`. Drives compatibility rules below. |
| `eventType` | Discriminator (`file.transfer.requested` today). |
| `eventId` | Unique per publish attempt. Not used for dedupe — changes on republish. |
| `correlationId` | Stable per file journey. Carried into every log line and into the DLQ envelope. |
| `idempotencyKey` | `sha256:<hex>` derived from source path + size + content checksum. Stable across redeliveries — this IS the dedupe key. |
| `occurredAt` | RFC 3339 UTC, when the watcher saw the file as stable. |
| `source` | Where the watcher found the file. |
| `file` | Name, size, SHA-256 checksum, content type — used for idempotency and post-upload integrity checks. |
| `destinations[]` | One entry per cloud target. Consumer fans these out **concurrently**; the event is only fully processed once every destination acks. |
| `retry` | Present only on redelivery: attempt number, max attempts, last error code. |

## 2. Failure & compatibility rules

### Retries / backoff
- The consumer processes a message, and on a **transient** failure (timeout, 5xx,
  connection error) it republishes the event to the same queue with `retry.attempt`
  incremented, after a delay published via a **per-message TTL on a dedicated retry
  queue** (`orders.inbound.retry`, dead-lettered back to `orders.inbound` on expiry).
- Backoff: `delay = RETRY_BACKOFF_BASE ^ attempt` seconds (default base `2`):
  attempt 1 → 2s, attempt 2 → 4s, attempt 3 → 8s, attempt 4 → 16s.
- `RETRY_MAX_ATTEMPTS` (default `5`). On the attempt that exceeds this, the message is
  routed to the **DLQ** (`orders.inbound.dlq`) instead of retried, with `x-death`
  headers plus `retry.lastError` populated.
- **Permanent** failures (schema validation failure, unsupported `schemaVersion`
  major, unknown destination `provider`) skip retries entirely and go straight to the
  DLQ — retrying a malformed message can't fix it.
- Idempotency: before uploading, the consumer checks `idempotencyKey` against a local
  store (SQLite). If already marked `done`, the message is ack'd as a no-op. This makes
  redeliveries (broker crash, requeue after worker restart) safe.

### Versioning & deprecation
- `schemaVersion` is `MAJOR.MINOR`.
  - **MINOR** (e.g. `1.0` → `1.1`): additive, optional fields only. Consumers ignore
    unknown optional fields. No coordination required.
  - **MAJOR** (e.g. `1.x` → `2.0`): may remove/rename/retype fields. Consumers MUST
    reject events whose major version they don't support, routing them to the DLQ with
    `lastError = "unsupported_schema_version"` rather than crash-looping.
  - **Deprecation**: when bumping MAJOR, the previous MAJOR is supported for a fixed
    window (suggested: 90 days / one quarter), during which the old-version code path
    logs `WARN "schema version N is deprecated, remove by <date>"` on every message.

## 3. Identity & auth posture

- **No long-lived static credentials in any non-local environment.**
- **AWS**: worker assumes an IAM role via **OIDC/Workload Identity Federation (IRSA on
  EKS, or `AssumeRoleWithWebIdentity` elsewhere)**. The role is scoped to
  `s3:PutObject` / `s3:GetObject` on the single `wms-orders-processed/*` prefix it
  needs — nothing account-wide.
- **GCP**: worker authenticates via **Workload Identity Federation** (no service
  account key files). The bound identity has `roles/storage.objectCreator` on the
  single target bucket only.
- **Local/dev (this repo)**: MinIO + fake-gcs-server use static dev credentials defined
  in `.env` (gitignored, `.env.example` provided). These are clearly dev-only and
  never valid outside `docker compose`.
- **Secrets**: broker credentials and any non-WIF secrets are injected via environment
  variables from a secrets manager (e.g. AWS Secrets Manager / GCP Secret Manager) at
  deploy time — never committed, never baked into images.
- **Least privilege** extends to the broker too: the worker's RabbitMQ user has
  `configure/write/read` only on the `orders.*` queues/exchanges, not the full vhost.

## 4. Run profile

- **Logs**: structured JSON to stdout. Every line includes
  `timestamp, level, message, service, correlationId, eventId, idempotencyKey` (when
  available) plus `attempt` during retries. No file-based logging inside the
  container — the runtime (Docker/K8s) owns log shipping.
- **Metrics**: Prometheus-compatible `/metrics` endpoint (port 8080) exposing:
  - `events_published_total`, `events_consumed_total{result}`
  - `upload_duration_seconds{provider}` (histogram)
  - `uploads_total{provider,result}`
  - `dlq_messages_total`
  - `idempotency_hits_total`
- **Health**:
  - `/health` — liveness: process is up, can reach the broker.
  - `/ready` — readiness: broker connection open AND both storage clients
    initialised. Used for the container `HEALTHCHECK` and orchestrator readiness probe.

## 5. SLOs

| SLI | SLO | Rationale |
|---|---|---|
| **Detection-to-publish latency** — time from file becoming stable on `/watch` to the event being durably published to the broker | **p95 ≤ 5s** | Keeps the event-driven path competitive with (and a precursor to replacing) the 15-minute polling export; failures here block everything downstream. |
| **End-to-end transfer success** — fraction of `file.transfer.requested` events for which **both** destinations report a successful, checksum-verified copy within `RETRY_MAX_ATTEMPTS` | **≥ 99.5% within 2 minutes**, measured over a rolling 24h window; anything landing in the DLQ counts against this | Directly protects against silent data loss / partial copies, the core risk this worker exists to mitigate. |
