# sftp-event-worker

A solution to a two-part "Cloud Engineer Challenge":

- **[`EXERCISE-1.md`](EXERCISE-1.md)** - a discovery/diagnosis pack for an
  overselling problem in a multi-channel retail setup (storefront + eBay +
  Amazon, SFTP/CSV order aggregation, 15-minute inventory sync).
- **Exercise 2 (this build)** - a working, local, event-driven worker: a file
  landing on an SFTP server triggers a RabbitMQ event (with retry/DLQ), which
  a consumer picks up and concurrently copies to AWS S3 (MinIO locally) and
  GCP GCS (fake-gcs-server locally).

## Quickstart

Requires Docker + Docker Compose.

```sh
make up          # build images and start the full stack (detached)
make smoke       # wait for health, drop a test order file, verify it lands in both clouds
make drop-file   # drop another test file against the running stack
make logs        # tail logs for everything
make down-v      # stop and remove all volumes (full reset)
```

Once `make up` finishes:

| Service | URL |
|---|---|
| Worker health/ready/metrics | http://localhost:8080/health, /ready, /metrics |
| RabbitMQ management UI | http://localhost:15672 (guest/guest) |
| MinIO console | http://localhost:9001 (minioadmin/minioadmin) |
| fake-gcs JSON API | http://localhost:4443/storage/v1/b |
| SFTP | `sftp -P 2222 sftpuser@localhost` (password: `demopass`) |

Drop any file into `/home/sftpuser/upload` on the SFTP server (or via
`make drop-file`) and watch `make logs` - the watcher publishes an event, the
consumer uploads the file to both `wms-orders-processed` buckets, and
`/metrics` reflects the result.

## Architecture

```
SFTP --(inotify)--> watcher --> RabbitMQ (orders.inbound + retry/DLQ) --> consumer --> MinIO (S3) + fake-gcs (GCS)
```

The watcher and consumer are two threads of one Python process
(`worker/src/main.py`), each with its own RabbitMQ connection. Retries use a
TTL + dead-letter-exchange queue (no delayed-message plugin needed); an
HMAC-derived idempotency key + local SQLite store make redeliveries safe.

Full diagram, component breakdown, and design rationale:
**[`docs/architecture.md`](docs/architecture.md)**. Design decisions and
trade-offs: **[`ADR.md`](ADR.md)**. Operational guide (logs, metrics, DLQ
inspection, troubleshooting): **[`docs/runbook.md`](docs/runbook.md)**.

## Event contract, identity posture & SLOs

The full contract - schema, retry/versioning rules, identity/auth posture, and
SLOs - is in **[`docs/event-contract.md`](docs/event-contract.md)**, backed by
[`schemas/transfer-event.schema.json`](schemas/transfer-event.schema.json).
Example event:

```json
{
  "schemaVersion": "1.0",
  "eventType": "file.transfer.requested",
  "eventId": "8f14e45f-ceea-4c4f-9f2a-1d2b3c4d5e6f",
  "correlationId": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "idempotencyKey": "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
  "occurredAt": "2026-06-10T09:32:11Z",
  "source": { "provider": "sftp", "path": "/watch/orders/ORD-20260610-0001.csv" },
  "file": { "name": "ORD-20260610-0001.csv", "sizeBytes": 4096, "checksumSha256": "9f86...", "contentType": "text/csv" },
  "destinations": [
    { "provider": "aws-s3", "bucket": "wms-orders-processed", "key": "orders/ORD-20260610-0001.csv", "region": "us-east-1" },
    { "provider": "gcp-gcs", "bucket": "wms-orders-processed", "key": "orders/ORD-20260610-0001.csv" }
  ]
}
```

In production, identity for both clouds would come from short-lived,
role-based credentials (IRSA / Workload Identity Federation) scoped to this
one bucket - sketched (illustratively, non-deployable) in
[`infra/terraform/`](infra/terraform/).

## Testing

```sh
make test    # ruff + bandit + pip-audit + pytest --cov, in worker/.venv (bootstrapped on first run)
make lint    # ruff only
```

53 unit tests cover the schema, watcher, publisher, consumer (happy path,
duplicate, retry, DLQ), uploader, idempotency store, health endpoints, and
logging. CI (`.github/workflows/ci.yml`) runs lint+test and security scans on
every push/PR, builds all four images, and runs the same `make smoke` flow
end-to-end.

## Repo structure

| Path | Contents |
|---|---|
| `worker/src/` | The worker: watcher, publisher, consumer, uploader, idempotency store, health/metrics server, config, entrypoint. |
| `worker/tests/` | Unit tests (pytest). |
| `docker/` | One Dockerfile per service (`worker`, `sftp`, `rabbitmq`, `fake-gcs`). |
| `infra/docker-compose.yml` | The whole local stack. |
| `infra/terraform/` | Illustrative-only IaC mapping the local setup to real AWS/GCP IAM. |
| `schemas/` | JSON Schema for the event contract + examples. |
| `docs/` | Architecture, event contract, and runbook. |
| `scripts/` | `run-tests.sh`, `healthcheck.sh`, `drop-test-file.sh` (used by the `Makefile` and CI). |
| `EXERCISE-1.md`, `ADR.md` | Exercise 1 write-up and the architecture decision record for Exercise 2. |

## Assumptions & limitations

- Local emulators (MinIO, fake-gcs-server, atmoz/sftp) stand in for real
  AWS/GCP/SFTP - see `infra/terraform/` for what a real deployment's IAM would
  look like.
- The idempotency store is a single SQLite file on a Docker volume, suitable
  for one worker instance; scaling out would need a shared store (see
  `ADR.md`).
- The watcher assumes files are written via a single `close`/`rename` (true
  for SFTP clients writing whole files); chunked/resumable uploads aren't
  handled.
- SFTP credentials in `infra/docker-compose.yml`/`.env.example` are demo-only,
  intentionally not secret.
