# Illustrative only - see README.md.
#
# Reacts to new CSVs landing under orders/ in the bucket from aws.tf (the
# same objects worker/src/uploader.py writes), parses each row, and writes
# one item per row into a DynamoDB table. See ADR.md (ADR 0002) for the
# design rationale and alternatives considered.

resource "aws_dynamodb_table" "order_line_items" {
  name         = var.order_line_items_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "orderId"
  range_key    = "sku"

  attribute {
    name = "orderId"
    type = "S"
  }

  attribute {
    name = "sku"
    type = "S"
  }
}

# Failed async (S3-triggered) Lambda invocations land here for manual
# inspection - the Lambda analogue of orders.inbound.dlq.
resource "aws_sqs_queue" "order_csv_to_dynamodb_dlq" {
  name                      = "order-csv-to-dynamodb-dlq"
  message_retention_seconds = 1209600 # 14 days
}

data "archive_file" "order_csv_to_dynamodb" {
  type        = "zip"
  source_dir  = "${path.module}/functions/aws_order_csv_to_dynamodb"
  output_path = "${path.module}/.build/order_csv_to_dynamodb.zip"
}

data "aws_iam_policy_document" "order_csv_to_dynamodb_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "order_csv_to_dynamodb" {
  name               = "order-csv-to-dynamodb"
  assume_role_policy = data.aws_iam_policy_document.order_csv_to_dynamodb_assume_role.json
}

resource "aws_iam_role_policy_attachment" "order_csv_to_dynamodb_logs" {
  role       = aws_iam_role.order_csv_to_dynamodb.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Least privilege: read the source objects, write only this table, send only
# to this DLQ - no account-wide s3:*/dynamodb:*.
data "aws_iam_policy_document" "order_csv_to_dynamodb_permissions" {
  statement {
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.orders_processed.arn}/orders/*"]
  }

  statement {
    effect    = "Allow"
    actions   = ["dynamodb:PutItem", "dynamodb:BatchWriteItem"]
    resources = [aws_dynamodb_table.order_line_items.arn]
  }

  statement {
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.order_csv_to_dynamodb_dlq.arn]
  }
}

resource "aws_iam_role_policy" "order_csv_to_dynamodb" {
  name   = "order-csv-to-dynamodb"
  role   = aws_iam_role.order_csv_to_dynamodb.id
  policy = data.aws_iam_policy_document.order_csv_to_dynamodb_permissions.json
}

resource "aws_lambda_function" "order_csv_to_dynamodb" {
  function_name    = "order-csv-to-dynamodb"
  role             = aws_iam_role.order_csv_to_dynamodb.arn
  handler          = "handler.handle_s3_event"
  runtime          = "python3.12"
  timeout          = 30
  memory_size      = 256
  filename         = data.archive_file.order_csv_to_dynamodb.output_path
  source_code_hash = data.archive_file.order_csv_to_dynamodb.output_base64sha256

  environment {
    variables = {
      TABLE_NAME = aws_dynamodb_table.order_line_items.name
    }
  }

  # Async (S3) invocations that fail all retries are sent here instead of
  # being silently dropped.
  dead_letter_config {
    target_arn = aws_sqs_queue.order_csv_to_dynamodb_dlq.arn
  }
}

resource "aws_lambda_permission" "order_csv_to_dynamodb_s3" {
  statement_id  = "AllowS3Invoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.order_csv_to_dynamodb.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.orders_processed.arn
}

resource "aws_s3_bucket_notification" "orders_processed" {
  bucket = aws_s3_bucket.orders_processed.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.order_csv_to_dynamodb.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "orders/"
    filter_suffix       = ".csv"
  }

  depends_on = [aws_lambda_permission.order_csv_to_dynamodb_s3]
}
