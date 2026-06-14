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

---

# ADR 0002: Serverless CSV-to-NoSQL ingestion per cloud

**Status:** Accepted

## Context

Once the worker lands an `orders/*.csv` file in S3 and an equivalent object in
GCS (ADR 0001), nothing reads those files back out - they just sit in object
storage. A natural next step for a WMS demo is to parse each CSV's order lines
and make them queryable, in each cloud, without standing up a database server.
The brief asks for: a Lambda/Cloud Run-or-Functions handler per cloud, a
"simple NoSQL db" per cloud, and Terraform for the lot, evaluating the
relevant AWS/GCP services first.

The CSV format (per `scripts/drop-test-file.sh`) is a header row
`order_id,sku,quantity,channel` followed by one line per order item - i.e. a
denormalized order-line-item export, not a single-row-per-order summary.

## Decision

**One small, directly-triggered function per cloud, writing to that cloud's
serverless document/NoSQL store, keyed on `(orderId, sku)`:**

- **AWS**: `aws_s3_bucket_notification` on the existing `orders_processed`
  bucket fires `order-csv-to-dynamodb` (Lambda, Python 3.12,
  `s3:ObjectCreated:*` on `orders/*.csv`) directly - no intermediate queue.
  The function streams the object from S3, parses it with `csv.DictReader`,
  and `batch_writer().put_item()`s one row per line item into a
  **DynamoDB** table (`order-line-items`, `PAY_PER_REQUEST`, partition key
  `orderId`, sort key `sku`). Failed async invocations go to an SQS DLQ via
  `dead_letter_config`.
- **GCP**: a **Cloud Functions (2nd gen)** function `order-csv-to-firestore`
  (Python 3.12) is deployed with a built-in Eventarc trigger on
  `google.cloud.storage.object.v1.finalized` for the `orders_processed`
  bucket, filtered in-code to `orders/*.csv`. It downloads the object, parses
  it the same way, and `set()`s one **Firestore** (Native mode) document per
  row at `orders/{orderId}/lineItems/{sku}`.
- **Idempotency by overwrite, not dedupe.** Both functions key writes on the
  CSV's own `(order_id, sku)` columns, so a redelivered S3/GCS notification
  (or a re-uploaded file with the same rows) simply overwrites the same
  item/document with the same data - no separate dedupe table, mirroring the
  `collision_mode: overwrite` precedent from the Benthos stream pipeline
  (`guide/13-benthos.md`).

## Alternatives considered & rejected

- **AWS: EventBridge → Step Functions / Glue / EMR Serverless.** All three are
  built for orchestrating multi-stage or large-scale (Spark-size) data
  pipelines. A single small CSV per file is a one-function job; adding a
  state machine, a Glue catalog, or a Spark cluster would be infrastructure
  the demo never exercises.
- **AWS: SNS/SQS fan-out in front of the Lambda.** Useful when multiple
  consumers need the same S3 event, but there is exactly one consumer here.
  `aws_s3_bucket_notification`'s native `lambda_function` block is simpler and
  one resource fewer.
- **AWS: DynamoDB vs. Aurora Serverless v2 / RDS / Timestream.** Aurora/RDS
  need a VPC, subnet group, and a always-billed (or scale-to-zero-with-cold-
  starts) instance - heavy for "insert parsed rows". Timestream is
  purpose-built for time-series metrics, not order line items. DynamoDB's
  on-demand mode bills per request with zero idle cost and needs no network
  plumbing, matching "simple NoSQL db".
- **GCP: Cloud Run vs. Cloud Functions (2nd gen).** 2nd-gen Cloud Functions
  *are* Cloud Run under the hood, but `google_cloudfunctions2_function`'s
  `event_trigger` block wires the Eventarc GCS trigger (and its Pub/Sub
  plumbing) for you. A hand-rolled Cloud Run service would need a separate
  `google_eventarc_trigger` resource pointed at it - more resources for the
  same outcome, so Cloud Functions 2nd gen was chosen for this demo.
- **GCP: Dataflow.** Apache Beam/Dataflow is for large, possibly-streaming,
  possibly-multi-source transforms with autoscaling workers - far beyond
  parsing one small CSV per upload.
- **GCP: Firestore vs. Bigtable / Cloud SQL.** Bigtable's minimum cluster
  pricing (and its wide-column model) is overkill for a handful of order
  documents. Cloud SQL needs a provisioned instance and VPC connector for a
  serverless function to reach it. Firestore Native mode is fully serverless,
  bills per operation, and its document/subcollection model
  (`orders/{orderId}/lineItems/{sku}`) maps naturally onto an order with many
  line items.

## Consequences / trade-offs

- **DLQ asymmetry between clouds.** `aws_lambda_function.dead_letter_config`
  is one extra resource (an SQS queue). The Cloud Functions 2nd gen
  `event_trigger` has no equivalent `dead_letter_config` - a real DLQ would
  require hand-wiring a `google_eventarc_trigger` with a Pub/Sub topic and
  `dead_letter_policy`, plus granting Pub/Sub's service agent
  `roles/pubsub.publisher` on that topic. This is called out as a known gap
  rather than implemented, to keep the GCP side at the same scope as the AWS
  side.
- **Schema coupling.** The CSV's column names (`order_id`, `sku`, `quantity`,
  `channel`) flow directly into both the DynamoDB item's and the Firestore
  document's field names. Changing the CSV format produced by
  `worker/src/uploader.py` (or `scripts/drop-test-file.sh`) requires a
  coordinated change to both `functions/aws_order_csv_to_dynamodb/handler.py`
  and `functions/gcp_order_csv_to_firestore/main.py` - there is no schema
  registry between them.
- **Eventarc/Pub/Sub IAM plumbing is GCP-only ceremony.** The
  `google_project_iam_member.gcs_pubsub_publisher` grant
  (`roles/pubsub.publisher` for the GCS service agent) exists purely so
  Eventarc's GCS trigger can receive finalize notifications - there is no AWS
  equivalent, since `aws_s3_bucket_notification` talks to Lambda directly.
- **Both functions are least-privilege and pay-per-use**: read-only on the
  source bucket prefix, write-only on their own table/collection, and (on
  AWS) send-only to their own DLQ - with no provisioned capacity sitting idle
  between file drops.
- **Still illustrative, not deployable as-is.** `aws_lambda.tf` and
  `gcp_function.tf` follow ADR 0001's existing stub conventions (no
  `required_providers`/`provider`/backend blocks) - see
  `infra/terraform/README.md`.

---

# ADR 0003: Managed SFTP ingestion via AWS Transfer Family (S3-direct)

**Status:** Proposed

## Context

ADR 0001's worker requires running and maintaining its own SFTP server
(`atmoz/sftp`) plus a watcher/consumer that detects completed uploads and
copies them into S3 via RabbitMQ. For the AWS leg specifically, that's four
moving pieces - SFTP container, broker, worker process, and the worker's S3
upload code - to get a file that's already arriving over SFTP into S3.

[AWS Transfer Family](https://aws.amazon.com/aws-transfer-family/) offers a
fully managed SFTP/FTPS/FTP endpoint whose storage backend *is* an S3 bucket
(`domain = "S3"`). A file uploaded via SFTP to a Transfer Family server
appears in S3 the moment the `PUT` completes - no watcher, no broker, no
upload step. This ADR is **Proposed**, not Accepted: it's an alternative
worth trying for the AWS leg, to be evaluated against ADR 0001's broker-first
design rather than assumed to replace it outright.

## Decision

Stand up `aws_transfer_server` (protocol `SFTP`, `identity_provider_type =
"SERVICE_MANAGED"`, `domain = "S3"`), with one `aws_transfer_user` whose
`home_directory_type = "LOGICAL"` maps the user's SFTP root directly onto
`orders/` inside the *existing* `aws_s3_bucket.orders_processed` bucket from
`aws.tf`. Authentication is via `aws_transfer_ssh_key` (public key only - AWS
never holds a private key).

Because files land under the same `orders/*.csv` prefix that
`aws_s3_bucket_notification.orders_processed` (ADR 0002) already watches,
**`order-csv-to-dynamodb` requires zero changes** - it fires exactly as it
does today, just triggered by an SFTP upload landing directly in S3 instead
of by a worker upload.

## Alternatives considered & rejected

- **AWS Transfer Family *Connector* (`aws_transfer_connector`), not Server.**
  A Connector is the wrong shape here: it's an AWS-side resource that
  *initiates* transfers to/from a *remote, third-party* SFTP server (via
  `StartFileTransfer`, typically on a schedule) - useful if a partner runs
  their own SFTP server and AWS needs to pull from it. Here, AWS itself needs
  to *be* the SFTP endpoint partners upload to - that's a Server.
- **`identity_provider_type = "API_GATEWAY"` or `"AWS_DIRECTORY_SERVICE"`**
  for custom/AD-backed auth. Both add real infrastructure (an API Gateway +
  Lambda authorizer, or a Directory Service directory) for credential
  management this demo doesn't need - `SERVICE_MANAGED` gives AWS-hosted SSH
  key storage per user with one extra resource
  (`aws_transfer_ssh_key`).
- **Self-hosted SFTP-to-S3 sync** (`atmoz/sftp` + a cron job running `aws s3
  sync`/`rclone`). Keeps an always-on container and adds polling latency;
  Transfer Family is pay-per-use (per-hour endpoint + per-GB transferred)
  with no idle container and no polling delay.

## Consequences / trade-offs

- **No local emulator.** Unlike everything else in `infra/terraform/`,
  Transfer Family has no MinIO-style local stand-in - it can only be
  illustrated here, not exercised via `docker compose`. Trying it for real
  needs an actual AWS account.
- **The GCS leg loses its trigger.** ADR 0001's worker watched one directory
  and uploaded to *both* clouds. If SFTP moves to Transfer Family (S3-only),
  nothing uploads to GCS. The natural fix is a small **S3-triggered Lambda
  that replicates `orders/*.csv` to the GCS `orders_processed` bucket** - the
  same `aws_s3_bucket_notification` mechanism as `order-csv-to-dynamodb`, a
  new function alongside it. Not built in this pass.
- **RabbitMQ/the worker become optional for the AWS leg**, but ADR 0001's
  retry/DLQ/idempotency guarantees don't disappear with them - they'd need to
  be re-implemented (more simply) inside the replication Lambda above for the
  GCS leg to keep the same guarantees. This is the main open question if this
  direction is pursued further.
- **Per-user IAM scoping.** Each `aws_transfer_user` needs its own
  `aws_iam_role`/`aws_transfer_ssh_key` - onboarding a second partner feed
  (e.g. a separate eBay drop) means one more user/role/key, not a config
  change to a shared worker.
- **Still illustrative, not deployable as-is** - `aws_transfer.tf` follows the
  same stub conventions as `aws.tf`/`aws_lambda.tf` (no
  `required_providers`/`provider`/backend blocks).
