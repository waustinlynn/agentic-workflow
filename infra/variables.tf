variable "aws_region" {
  description = "AWS region for all control-plane resources."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Base name prefix for AWS resources."
  type        = string
  default     = "agentic-workflow"
}

variable "environment" {
  description = "Deployment environment label (e.g., prod, dev)."
  type        = string
  default     = "prod"
}

variable "allowed_actors" {
  description = "GitHub logins permitted to trigger webhook ingestion on public repositories."
  type        = list(string)
  default = [
    "waustinlynn",
    "agentic-coder[bot]",
    "agentic-reviewer[bot]",
  ]
}

variable "sqs_visibility_timeout_seconds" {
  description = "Initial SQS FIFO visibility timeout (workers extend via heartbeat)."
  type        = number
  default     = 120

  validation {
    condition     = var.sqs_visibility_timeout_seconds >= 30 && var.sqs_visibility_timeout_seconds <= 43200
    error_message = "sqs_visibility_timeout_seconds must be between 30 and 43200."
  }
}

variable "sqs_max_receive_count" {
  description = "Maximum SQS FIFO delivery attempts before routing a message to the DLQ."
  type        = number
  default     = 2

  validation {
    condition     = var.sqs_max_receive_count >= 1 && var.sqs_max_receive_count <= 10
    error_message = "sqs_max_receive_count must be between 1 and 10."
  }
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention period in days for Lambda functions."
  type        = number
  default     = 14
}

variable "enable_dynamodb_pitr" {
  description = "Enable DynamoDB point-in-time recovery (adds small ongoing storage fee when true)."
  type        = bool
  default     = false
}

variable "create_local_runner_iam_user" {
  description = "Create a dedicated IAM user for the Phase 1 local runner (access keys are created out-of-band)."
  type        = bool
  default     = true
}

variable "initial_repos" {
  description = "Optional map of onboarded repositories to seed into the DynamoDB Repo Registry."
  type = map(object({
    message_group_id = string
    execution_target = optional(string, "local")
    runtime          = optional(string, "base")
    enabled          = optional(bool, true)
  }))
  default = {}

  validation {
    condition = alltrue([
      for repo, cfg in var.initial_repos :
      can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", repo)) &&
      contains(["local", "lambda"], cfg.execution_target) &&
      contains(["base", "python", "node", "dotnet", "rust", "go", "infra", "ci-verified"], cfg.runtime)
    ])
    error_message = "Each initial_repos key must be 'owner/repo' with valid execution_target and runtime."
  }
}
