# Terraform stub (illustrative only)

This directory sketches the **shape** of the cloud resources and identity
posture that `infra/docker-compose.yml`'s MinIO + fake-gcs emulators stand in
for locally. It has **no `required_providers`/`provider`/backend blocks and is
not intended to be `terraform init`/`plan`/`apply`'d** as-is - it exists to be
read, not deployed.

It illustrates, alongside
[`docs/event-contract.md`](../../docs/event-contract.md#3-identity--auth-posture):

- **`aws.tf`** - an S3 bucket (versioned, encrypted, public access blocked)
  plus an IAM role assumable only via IRSA (`AssumeRoleWithWebIdentity`,
  scoped to one EKS namespace + service account), with a policy limited to
  `s3:PutObject` / `s3:GetObject` / `s3:ListBucket` on that bucket only.
- **`gcp.tf`** - a GCS bucket (uniform bucket-level access, versioned) plus a
  service account granted `roles/storage.objectAdmin` on that bucket only,
  bound to a Workload Identity Federation pool so the worker never needs a
  downloadable JSON key.
- **`aws_lambda.tf`** - a Lambda function (`order-csv-to-dynamodb`, Python
  3.12) triggered by S3 `ObjectCreated` notifications on `orders/*.csv` in the
  bucket from `aws.tf`, plus the DynamoDB table it writes to
  (`order-line-items`, on-demand billing, keyed on `orderId`/`sku`) and an SQS
  dead-letter queue for failed invocations. Its source lives in
  [`functions/aws_order_csv_to_dynamodb/`](functions/aws_order_csv_to_dynamodb/).
- **`gcp_function.tf`** - a Cloud Function (2nd gen) `order-csv-to-firestore`
  (Python 3.12) triggered via Eventarc on object-finalize events for the
  bucket from `gcp.tf`, writing to a Firestore (Native mode) database at
  `orders/{orderId}/lineItems/{sku}`. Its source lives in
  [`functions/gcp_order_csv_to_firestore/`](functions/gcp_order_csv_to_firestore/).
  See [ADR 0002](../../ADR.md) for the design rationale, alternatives
  considered, and a known gap (no GCP-side DLQ equivalent).
- **`variables.tf`** / **`outputs.tf`** - the minimal inputs (bucket name,
  account/project IDs, OIDC/WIF identifiers, table name) and outputs (role
  ARN, service account email, bucket names, function names, table name, DLQ
  URL, Firestore database name) needed to wire a real deployment to these
  resources.
- **`aws_transfer.tf`** - an AWS Transfer Family SFTP server
  (`SERVICE_MANAGED` identity, `domain = "S3"`) with one user whose home
  directory maps straight to `orders/` in the bucket from `aws.tf` - a
  managed-SFTP alternative to running the `atmoz/sftp` container + worker for
  the AWS leg. See [ADR 0003](../../ADR.md) (Proposed) for the rationale,
  what this would replace, and the open question for the GCS leg.

In a real deployment, `S3_ENDPOINT_URL` / `GCS_ENDPOINT_URL` (see
`.env.example`) would be unset - falling back to the real AWS/GCP endpoints -
and credentials would come from the IRSA role / WIF binding defined here
instead of the `MINIO_ROOT_USER` / `AWS_ACCESS_KEY_ID` static dev values used
by the local emulators.
