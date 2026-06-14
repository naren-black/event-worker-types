# Illustrative only - see README.md.
#
# Managed SFTP endpoint backed directly by S3: a file uploaded via SFTP here
# appears under orders/ in the same aws_s3_bucket.orders_processed bucket
# from aws.tf - no watcher, no broker, no worker upload step. Because it's
# the same prefix aws_s3_bucket_notification.orders_processed (ADR 0002)
# already watches, order-csv-to-dynamodb fires unchanged. See ADR.md (ADR
# 0003) for the design rationale, what this replaces, and the open question
# it leaves for the GCS leg.

# Transfer Family writes session/transfer logs via this role - required even
# for the simplest SERVICE_MANAGED setup.
data "aws_iam_policy_document" "transfer_logging_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["transfer.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "transfer_logging" {
  name               = "sftp-event-worker-transfer-logging"
  assume_role_policy = data.aws_iam_policy_document.transfer_logging_assume_role.json
}

resource "aws_iam_role_policy_attachment" "transfer_logging" {
  role       = aws_iam_role.transfer_logging.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSTransferLoggingAccess"
}

resource "aws_transfer_server" "orders" {
  identity_provider_type = "SERVICE_MANAGED"
  protocols              = ["SFTP"]
  domain                 = "S3"
  endpoint_type          = "PUBLIC"
  logging_role           = aws_iam_role.transfer_logging.arn
}

# Least privilege: this user can only read/write/list under orders/ in the
# one bucket order-csv-to-dynamodb (ADR 0002) already watches - no
# account-wide s3:* and no access to other prefixes.
data "aws_iam_policy_document" "transfer_user_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["transfer.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "transfer_user" {
  name               = "sftp-event-worker-transfer-user"
  assume_role_policy = data.aws_iam_policy_document.transfer_user_assume_role.json
}

data "aws_iam_policy_document" "transfer_user_s3" {
  statement {
    effect    = "Allow"
    actions   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.orders_processed.arn}/orders/*"]
  }

  statement {
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.orders_processed.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["orders/*"]
    }
  }
}

resource "aws_iam_role_policy" "transfer_user_s3" {
  name   = "sftp-event-worker-transfer-user-s3"
  role   = aws_iam_role.transfer_user.id
  policy = data.aws_iam_policy_document.transfer_user_s3.json
}

# LOGICAL home directory: the SFTP user's "/" is mapped straight to orders/
# in the bucket, so `sftp put order.csv` lands at
# s3://<bucket>/orders/order.csv - exactly where the ADR 0002 Lambda's S3
# notification filter (prefix "orders/", suffix ".csv") expects it.
resource "aws_transfer_user" "partner" {
  server_id = aws_transfer_server.orders.id
  user_name = var.transfer_user_name
  role      = aws_iam_role.transfer_user.arn

  home_directory_type = "LOGICAL"

  home_directory_mappings {
    entry  = "/"
    target = "/${aws_s3_bucket.orders_processed.bucket}/orders"
  }
}

# SERVICE_MANAGED identity: AWS stores only the public key, the partner
# keeps the private key - no credential material ever lives in this
# Terraform state.
resource "aws_transfer_ssh_key" "partner" {
  server_id = aws_transfer_server.orders.id
  user_name = aws_transfer_user.partner.user_name
  body      = var.transfer_user_ssh_public_key
}
