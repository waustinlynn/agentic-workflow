output "deploy_role_arn" {
  description = "Set this as the AWS_DEPLOY_ROLE_ARN GitHub variable in each repo."
  value       = aws_iam_role.deploy.arn
}

output "oidc_provider_arn" {
  description = "GitHub OIDC provider ARN."
  value       = local.oidc_provider_arn
}

output "plan_role_arn" {
  description = "Set this as the AWS_PLAN_ROLE_ARN GitHub variable (null if github_plan_subjects is empty)."
  value       = local.create_plan_role ? aws_iam_role.plan[0].arn : null
}
