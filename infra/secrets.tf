# Secrets Manager containers only — secret values are populated out-of-band
# via AWS CLI / Console so sensitive material never enters Terraform code or state.

resource "aws_secretsmanager_secret" "github_webhook" {
  name                    = "${local.prefix}/github-webhook-secret"
  description             = "Shared HMAC secret for verifying GitHub App webhooks."
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret" "github_coder_app" {
  name                    = "${local.prefix}/github-coder-app"
  description             = "JSON {\"app_id\", \"private_key\"} credentials for agentic-coder[bot]."
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret" "github_reviewer_app" {
  name                    = "${local.prefix}/github-reviewer-app"
  description             = "JSON {\"app_id\", \"private_key\"} credentials for agentic-reviewer[bot]."
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret" "antigravity_api_key" {
  name                    = "${local.prefix}/antigravity-api-key"
  description             = "Headless Antigravity/Gemini API key for Phase 2 AWS Lambda workers."
  recovery_window_in_days = 7
}
