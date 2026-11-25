locals {
  ingest_zip_path = "${path.module}/.build/ingest_lambda.zip"
}

resource "aws_cloudwatch_log_group" "ingest" {
  name              = "/aws/lambda/${local.prefix}-webhook-ingest"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ingest_lambda" {
  name               = "${local.prefix}-webhook-ingest-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

data "aws_iam_policy_document" "ingest_lambda" {
  statement {
    sid    = "CloudWatchLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.ingest.arn}:*"]
  }

  statement {
    sid     = "ReadWebhookAndCoderSecrets"
    effect  = "Allow"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.github_webhook.arn,
      aws_secretsmanager_secret.github_coder_app.arn,
    ]
  }

  statement {
    sid       = "ReadRepoRegistry"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.repo_registry.arn]
  }

  statement {
    sid       = "EnqueueTasks"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.tasks.arn]
  }
}

resource "aws_iam_role_policy" "ingest_lambda" {
  name   = "${local.prefix}-webhook-ingest-policy"
  role   = aws_iam_role.ingest_lambda.id
  policy = data.aws_iam_policy_document.ingest_lambda.json
}

resource "aws_lambda_function" "ingest" {
  function_name    = "${local.prefix}-webhook-ingest"
  description      = "Verifies GitHub webhooks, checks allowlists, and enqueues repo-scoped SQS FIFO tasks."
  role             = aws_iam_role.ingest_lambda.arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "ingest.handler.lambda_handler"
  filename         = local.ingest_zip_path
  source_code_hash = fileexists(local.ingest_zip_path) ? filebase64sha256(local.ingest_zip_path) : null
  timeout          = 15
  memory_size      = 256

  environment {
    variables = {
      SQS_QUEUE_URL        = aws_sqs_queue.tasks.url
      REPO_REGISTRY_TABLE  = aws_dynamodb_table.repo_registry.name
      WEBHOOK_SECRET_ARN   = aws_secretsmanager_secret.github_webhook.arn
      CODER_APP_SECRET_ARN = aws_secretsmanager_secret.github_coder_app.arn
      ALLOWED_ACTORS       = join(",", var.allowed_actors)
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.ingest,
    aws_iam_role_policy.ingest_lambda,
  ]
}

resource "aws_apigatewayv2_api" "webhook" {
  name          = "${local.prefix}-webhook-api"
  protocol_type = "HTTP"
  description   = "Public HTTPS ingress for GitHub App webhooks."
}

resource "aws_apigatewayv2_integration" "ingest" {
  api_id                 = aws_apigatewayv2_api.webhook.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.ingest.invoke_arn
  integration_method     = "POST"
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "github_webhook" {
  api_id    = aws_apigatewayv2_api.webhook.id
  route_key = "POST /webhook/github"
  target    = "integrations/${aws_apigatewayv2_integration.ingest.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.webhook.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_burst_limit = 20
    throttling_rate_limit  = 10
  }
}

resource "aws_lambda_permission" "apigw_invoke" {
  statement_id  = "AllowAPIGatewayInvokeWebhook"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ingest.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.webhook.execution_arn}/*/*/webhook/github"
}
