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

variable "enable_worker_ecr_repos" {
  description = "Provision ECR repositories for Phase 2 Lambda worker container images ($0 idle cost when empty)."
  type        = bool
  default     = true
}

variable "enable_lambda_workers" {
  description = "Provision Phase 2 per-runtime SQS FIFO queues, Lambda container functions, and Event Source Mappings (enable after pushing images to ECR)."
  type        = bool
  default     = false
}

variable "lambda_worker_runtimes" {
  description = "Stack runtime profiles provisioned as Phase 2 Lambda container images (must include 'base')."
  type        = list(string)
  default     = ["base", "python", "node", "infra"]

  validation {
    condition = (
      contains(var.lambda_worker_runtimes, "base") &&
      alltrue([
        for rt in var.lambda_worker_runtimes :
        contains(["base", "python", "node", "infra", "dotnet", "rust", "go"], rt)
      ])
    )
    error_message = "lambda_worker_runtimes must include 'base' and only supported stack runtime names."
  }
}

variable "lambda_worker_image_tag" {
  description = "Default ECR image tag used for Phase 2 Lambda container workers when not overridden in lambda_worker_image_uris."
  type        = string
  default     = "latest"
}

variable "lambda_worker_image_uris" {
  description = "Optional per-runtime image URI overrides (e.g., pinned immutable @sha256 digests)."
  type        = map(string)
  default     = {}
}

variable "lambda_worker_architecture" {
  description = "CPU architecture for Phase 2 Lambda container workers (must match pushed Docker images)."
  type        = string
  default     = "arm64"

  validation {
    condition     = contains(["arm64", "x86_64"], var.lambda_worker_architecture)
    error_message = "lambda_worker_architecture must be 'arm64' or 'x86_64'."
  }
}

variable "lambda_worker_timeout_seconds" {
  description = "Hard AWS Lambda timeout for Phase 2 worker functions (max 900s = 15 minutes)."
  type        = number
  default     = 900

  validation {
    condition     = var.lambda_worker_timeout_seconds >= 60 && var.lambda_worker_timeout_seconds <= 900
    error_message = "lambda_worker_timeout_seconds must be between 60 and 900."
  }
}

variable "lambda_worker_soft_timeout_buffer_seconds" {
  description = "Soft-timeout safety buffer before Lambda hard timeout so workers cleanly label agent:needs-human."
  type        = number
  default     = 120

  validation {
    condition     = var.lambda_worker_soft_timeout_buffer_seconds >= 30 && var.lambda_worker_soft_timeout_buffer_seconds <= 300
    error_message = "lambda_worker_soft_timeout_buffer_seconds must be between 30 and 300."
  }
}

variable "lambda_worker_memory_mb" {
  description = "Memory allocation (MB) for Phase 2 Lambda container workers."
  type        = number
  default     = 3008

  validation {
    condition     = var.lambda_worker_memory_mb >= 512 && var.lambda_worker_memory_mb <= 10240
    error_message = "lambda_worker_memory_mb must be between 512 and 10240."
  }
}

variable "lambda_worker_ephemeral_storage_mb" {
  description = "Writable /tmp ephemeral storage (MB) for git checkouts and build caches."
  type        = number
  default     = 2048

  validation {
    condition     = var.lambda_worker_ephemeral_storage_mb >= 512 && var.lambda_worker_ephemeral_storage_mb <= 10240
    error_message = "lambda_worker_ephemeral_storage_mb must be between 512 and 10240."
  }
}

variable "lambda_worker_max_concurrency" {
  description = "Maximum concurrent Lambda invocations per runtime SQS FIFO Event Source Mapping (AWS minimum is 2)."
  type        = number
  default     = 5

  validation {
    condition     = var.lambda_worker_max_concurrency >= 2 && var.lambda_worker_max_concurrency <= 100
    error_message = "lambda_worker_max_concurrency must be between 2 and 100."
  }
}

variable "lambda_worker_agent_backend" {
  description = "Antigravity backend inside Phase 2 Lambda containers ('sdk' or 'agy_cli')."
  type        = string
  default     = "sdk"

  validation {
    condition     = contains(["sdk", "agy_cli"], var.lambda_worker_agent_backend)
    error_message = "lambda_worker_agent_backend must be 'sdk' or 'agy_cli'."
  }
}

variable "lambda_worker_model" {
  description = "Optional Antigravity/Gemini model override passed as ANTIGRAVITY_MODEL to Phase 2 Lambda workers."
  type        = string
  default     = ""
}
