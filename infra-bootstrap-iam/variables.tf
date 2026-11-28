variable "aws_account_id" {
  description = "AWS account that hosts the deploy role (guards against applying to the wrong account)."
  type        = string
  default     = "367553824651"
}

variable "aws_region" {
  description = "AWS region for the provider and state bucket."
  type        = string
  default     = "us-east-1"
}

variable "role_name" {
  description = "Name of the shared GitHub Actions deploy role."
  type        = string
  default     = "github-actions-deploy"
}

variable "github_subjects" {
  description = <<-EOT
    GitHub OIDC `sub` claim patterns allowed to assume the deploy role. Add one entry per repo/ref/environment, e.g.
    "repo:waustinlynn/agentic-workflow:ref:refs/heads/main",
    "repo:waustinlynn/agentic-workflow:environment:production",
    "repo:waustinlynn/agentic-workflow:pull_request" (plan-only; see README),
    "repo:waustinlynn/monitor-grow:ref:refs/heads/main".
  EOT
  type        = list(string)

  validation {
    condition     = length(var.github_subjects) > 0 && alltrue([for s in var.github_subjects : startswith(s, "repo:waustinlynn/")])
    error_message = "github_subjects must be non-empty and every entry must start with \"repo:waustinlynn/\"."
  }
}

variable "create_oidc_provider" {
  description = "Create the GitHub OIDC provider. Set false if one already exists in the account (it is one per account)."
  type        = bool
  default     = true
}

variable "resource_name_prefixes" {
  description = "Name prefixes of resources the deploy role may manage (IAM roles/policies, Lambda, SQS, DynamoDB, ECR, secrets, logs)."
  type        = list(string)
  default     = ["agentic-workflow", "monitor-grow"]
}

variable "state_bucket_arns" {
  description = "ARNs of S3 buckets holding Terraform state for the pipelines (read/write incl. lockfiles)."
  type        = list(string)
  default = [
    "arn:aws:s3:::agentic-workflow-tfstate-367553824651-us-east-1",
  ]
}

variable "additional_policy_arns" {
  description = "Extra managed policy ARNs to attach (e.g. repo-specific permissions for monitor-grow)."
  type        = list(string)
  default     = []
}

variable "max_session_duration" {
  description = "Max role session duration in seconds."
  type        = number
  default     = 3600
}

variable "plan_role_name" {
  description = "Name of the read-only role assumed by pull_request workflows to run `terraform plan`."
  type        = string
  default     = "github-actions-plan"
}

variable "github_plan_subjects" {
  description = "OIDC `sub` patterns allowed to assume the read-only plan role (e.g. \"repo:waustinlynn/agentic-workflow:pull_request\")."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for s in var.github_plan_subjects : startswith(s, "repo:waustinlynn/")])
    error_message = "Every github_plan_subjects entry must start with \"repo:waustinlynn/\"."
  }
}
