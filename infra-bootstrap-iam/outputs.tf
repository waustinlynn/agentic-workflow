output "deploy_role_arn" {
  description = "Set this as the AWS_DEPLOY_ROLE_ARN GitHub variable in each repo."
  value       = aws_iam_role.deploy.arn
}

output "oidc_provider_arn" {
  description = "GitHub OIDC provider ARN."
  value       = local.oidc_provider_arn
}
