# Least-privilege IAM policy (and optional dedicated IAM user) for the Phase 1 local runner.
# Access keys are NOT created by Terraform so credentials never enter state.

data "aws_iam_policy_document" "local_runner" {
  statement {
    sid    = "PollTasksQueue"
    effect = "Allow"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility",
      "sqs:GetQueueAttributes",
    ]
    resources = [aws_sqs_queue.tasks.arn]
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
    sid    = "ReadRepoRegistry"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:Scan",
    ]
    resources = [aws_dynamodb_table.repo_registry.arn]
  }

  statement {
    sid     = "ReadAppSecrets"
    effect  = "Allow"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.github_coder_app.arn,
      aws_secretsmanager_secret.github_reviewer_app.arn,
      aws_secretsmanager_secret.antigravity_api_key.arn,
    ]
  }
}

resource "aws_iam_policy" "local_runner" {
  name        = "${local.prefix}-local-runner-policy"
  description = "Least-privilege permissions for the Phase 1 local agentic worker daemon."
  policy      = data.aws_iam_policy_document.local_runner.json
}

resource "aws_iam_user" "local_runner" {
  count = var.create_local_runner_iam_user ? 1 : 0
  name  = "${local.prefix}-local-runner"
}

resource "aws_iam_user_policy_attachment" "local_runner" {
  count      = var.create_local_runner_iam_user ? 1 : 0
  user       = aws_iam_user.local_runner[0].name
  policy_arn = aws_iam_policy.local_runner.arn
}
