output "webhook_url" {
  description = "Public HTTPS endpoint to configure on the agentic-coder GitHub App."
  value       = "${aws_apigatewayv2_api.webhook.api_endpoint}/webhook/github"
}

output "ingest_lambda_name" {
  description = "Name of the Webhook Ingest Lambda function."
  value       = aws_lambda_function.ingest.function_name
}

output "ingest_lambda_arn" {
  description = "ARN of the Webhook Ingest Lambda function."
  value       = aws_lambda_function.ingest.arn
}

output "sqs_tasks_queue_url" {
  description = "URL of the primary SQS FIFO task queue."
  value       = aws_sqs_queue.tasks.url
}

output "sqs_tasks_queue_arn" {
  description = "ARN of the primary SQS FIFO task queue."
  value       = aws_sqs_queue.tasks.arn
}

output "sqs_tasks_dlq_url" {
  description = "URL of the SQS FIFO dead-letter queue."
  value       = aws_sqs_queue.tasks_dlq.url
}

output "sqs_tasks_dlq_arn" {
  description = "ARN of the SQS FIFO dead-letter queue."
  value       = aws_sqs_queue.tasks_dlq.arn
}

output "dynamodb_run_state_table" {
  description = "DynamoDB table storing per-(repo, issue_or_pr) workflow run state."
  value       = aws_dynamodb_table.run_state.name
}

output "dynamodb_repo_registry_table" {
  description = "DynamoDB table mapping repo_full_name -> MessageGroupId and runtime settings."
  value       = aws_dynamodb_table.repo_registry.name
}

output "secret_arns" {
  description = "ARNs of Secrets Manager secrets (values are populated out-of-band)."
  value = {
    github_webhook      = aws_secretsmanager_secret.github_webhook.arn
    github_coder_app    = aws_secretsmanager_secret.github_coder_app.arn
    github_reviewer_app = aws_secretsmanager_secret.github_reviewer_app.arn
    antigravity_api_key = aws_secretsmanager_secret.antigravity_api_key.arn
  }
}

output "local_runner_policy_arn" {
  description = "Least-privilege managed IAM policy ARN for the Phase 1 local runner."
  value       = aws_iam_policy.local_runner.arn
}

output "local_runner_user_name" {
  description = "Dedicated IAM user name for the local runner (null if create_local_runner_iam_user=false)."
  value       = var.create_local_runner_iam_user ? aws_iam_user.local_runner[0].name : null
}

output "worker_ecr_repository_urls" {
  description = "Map of runtime name -> ECR repository URL for Phase 2 Lambda container images."
  value       = { for k, r in aws_ecr_repository.worker : k => r.repository_url }
}

output "worker_sqs_queue_urls" {
  description = "Map of runtime name -> SQS FIFO queue URL for Phase 2 Lambda workers."
  value       = { for k, q in aws_sqs_queue.worker_tasks : k => q.url }
}

output "worker_lambda_arns" {
  description = "Map of runtime name -> AWS Lambda container worker ARN."
  value       = { for k, fn in aws_lambda_function.worker : k => fn.arn }
}
