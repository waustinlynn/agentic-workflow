# Phase 2 serverless execution: ECR container repositories, per-runtime SQS FIFO queues,
# Lambda container worker functions, and SQS Event Source Mappings (batch_size = 1).

locals {
  worker_ecr_runtimes    = var.enable_worker_ecr_repos || var.enable_lambda_workers ? toset(var.lambda_worker_runtimes) : toset([])
  active_worker_runtimes = var.enable_lambda_workers ? toset(var.lambda_worker_runtimes) : toset([])
}

resource "aws_ecr_repository" "worker" {
  for_each = local.worker_ecr_runtimes

  name                 = "${local.prefix}-worker-${each.key}"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "worker" {
  for_each   = local.worker_ecr_runtimes
  repository = aws_ecr_repository.worker[each.key].name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after 7 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = {
          type = "expire"
        }
      },
      {
        rulePriority = 2
        description  = "Keep only the 5 most recent images per runtime repository"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 5
        }
        action = {
          type = "expire"
        }
      },
    ]
  })
}

resource "aws_sqs_queue" "worker_tasks" {
  for_each = local.active_worker_runtimes

  name                        = "${local.prefix}-worker-${each.key}.fifo"
  fifo_queue                  = true
  content_based_deduplication = false
  deduplication_scope         = "messageGroup"
  fifo_throughput_limit       = "perMessageGroupId"
  visibility_timeout_seconds  = var.lambda_worker_timeout_seconds + 60
  receive_wait_time_seconds   = 20
  message_retention_seconds   = 345600
  sqs_managed_sse_enabled     = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.tasks_dlq.arn
    maxReceiveCount     = var.sqs_max_receive_count
  })
}

resource "aws_cloudwatch_log_group" "worker" {
  for_each = local.active_worker_runtimes

  name              = "/aws/lambda/${local.prefix}-worker-${each.key}"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "worker_lambda" {
  count              = var.enable_lambda_workers ? 1 : 0
  name               = "${local.prefix}-worker-lambda-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

data "aws_iam_policy_document" "worker_lambda" {
  count = var.enable_lambda_workers ? 1 : 0

  statement {
    sid    = "CloudWatchLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [for lg in aws_cloudwatch_log_group.worker : "${lg.arn}:*"]
  }

  statement {
    sid    = "ConsumeWorkerQueues"
    effect = "Allow"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:ChangeMessageVisibility",
    ]
    resources = [for q in aws_sqs_queue.worker_tasks : q.arn]
  }

  statement {
    sid    = "ReadWriteRunState"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
    ]
    resources = [aws_dynamodb_table.run_state.arn]
  }

  statement {
    sid     = "ReadWorkerSecrets"
    effect  = "Allow"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.github_coder_app.arn,
      aws_secretsmanager_secret.github_reviewer_app.arn,
      aws_secretsmanager_secret.antigravity_api_key.arn,
    ]
  }
}

resource "aws_iam_role_policy" "worker_lambda" {
  count  = var.enable_lambda_workers ? 1 : 0
  name   = "${local.prefix}-worker-lambda-policy"
  role   = aws_iam_role.worker_lambda[0].id
  policy = data.aws_iam_policy_document.worker_lambda[0].json
}

resource "aws_lambda_function" "worker" {
  for_each = local.active_worker_runtimes

  function_name = "${local.prefix}-worker-${each.key}"
  description   = "Phase 2 agentic worker Lambda container for runtime '${each.key}'."
  role          = aws_iam_role.worker_lambda[0].arn
  package_type  = "Image"
  image_uri     = lookup(var.lambda_worker_image_uris, each.key, "${aws_ecr_repository.worker[each.key].repository_url}:${var.lambda_worker_image_tag}")
  architectures = [var.lambda_worker_architecture]
  timeout       = var.lambda_worker_timeout_seconds
  memory_size   = var.lambda_worker_memory_mb

  ephemeral_storage {
    size = var.lambda_worker_ephemeral_storage_mb
  }

  environment {
    variables = {
      RUN_STATE_TABLE                    = aws_dynamodb_table.run_state.name
      GITHUB_CODER_SECRET_ARN            = aws_secretsmanager_secret.github_coder_app.arn
      GITHUB_REVIEWER_SECRET_ARN         = aws_secretsmanager_secret.github_reviewer_app.arn
      ANTIGRAVITY_API_KEY_SECRET_ARN     = aws_secretsmanager_secret.antigravity_api_key.arn
      AGENT_BACKEND                      = var.lambda_worker_agent_backend
      LAMBDA_TIMEOUT_SECONDS             = tostring(var.lambda_worker_timeout_seconds)
      LAMBDA_SOFT_TIMEOUT_BUFFER_SECONDS = tostring(var.lambda_worker_soft_timeout_buffer_seconds)
      AGENTIC_WORKSPACE_DIR              = "/tmp/agentic-workspaces"
      LOG_FORMAT                         = "json"
      WORKER_RUNTIME                     = each.key
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.worker,
    aws_iam_role_policy.worker_lambda,
  ]
}

resource "aws_lambda_event_source_mapping" "worker" {
  for_each = local.active_worker_runtimes

  event_source_arn        = aws_sqs_queue.worker_tasks[each.key].arn
  function_name           = aws_lambda_function.worker[each.key].arn
  batch_size              = 1
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = var.lambda_worker_max_concurrency
  }
}
