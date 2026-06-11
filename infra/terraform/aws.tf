# Illustrative only - see README.md.
# Mirrors the bucket created locally by infra/docker-compose.yml's
# `minio-init` service, plus the IRSA-based identity posture described in
# docs/event-contract.md (section 3).

resource "aws_s3_bucket" "orders_processed" {
  bucket = var.bucket_name
}

resource "aws_s3_bucket_versioning" "orders_processed" {
  bucket = aws_s3_bucket.orders_processed.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "orders_processed" {
  bucket = aws_s3_bucket.orders_processed.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "orders_processed" {
  bucket = aws_s3_bucket.orders_processed.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# IRSA: the worker's pod assumes this role via OIDC - no static credentials.
# Trust is scoped to one namespace + service account, not the whole cluster.
data "aws_iam_policy_document" "worker_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [var.eks_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${var.eks_oidc_provider_url}:sub"
      values   = ["system:serviceaccount:${var.k8s_namespace}:${var.k8s_service_account}"]
    }
  }
}

resource "aws_iam_role" "worker" {
  name               = "sftp-event-worker"
  assume_role_policy = data.aws_iam_policy_document.worker_assume_role.json
}

# Least privilege: object read/write under this bucket only - no
# account-wide s3:* and no bucket-management permissions.
data "aws_iam_policy_document" "worker_s3" {
  statement {
    effect    = "Allow"
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["${aws_s3_bucket.orders_processed.arn}/*"]
  }

  statement {
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.orders_processed.arn]
  }
}

resource "aws_iam_role_policy" "worker_s3" {
  name   = "sftp-event-worker-s3"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker_s3.json
}
