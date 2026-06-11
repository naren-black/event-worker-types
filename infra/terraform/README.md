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
- **`variables.tf`** / **`outputs.tf`** - the minimal inputs (bucket name,
  account/project IDs, OIDC/WIF identifiers) and outputs (role ARN, service
  account email, bucket names) needed to wire a real deployment to these
  resources.

In a real deployment, `S3_ENDPOINT_URL` / `GCS_ENDPOINT_URL` (see
`.env.example`) would be unset - falling back to the real AWS/GCP endpoints -
and credentials would come from the IRSA role / WIF binding defined here
instead of the `MINIO_ROOT_USER` / `AWS_ACCESS_KEY_ID` static dev values used
by the local emulators.
