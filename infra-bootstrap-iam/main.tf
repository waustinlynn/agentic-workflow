data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = var.aws_region

  oidc_provider_arn = (
    var.create_oidc_provider
    ? aws_iam_openid_connect_provider.github[0].arn
    : "arn:aws:iam::${local.account_id}:oidc-provider/token.actions.githubusercontent.com"
  )

  name_patterns = flatten([for p in var.resource_name_prefixes : ["${p}-*", "${p}*"]])
  role_arns     = [for p in var.resource_name_prefixes : "arn:aws:iam::${local.account_id}:role/${p}*"]
  policy_arns   = [for p in var.resource_name_prefixes : "arn:aws:iam::${local.account_id}:policy/${p}*"]
  lambda_arns   = [for p in var.resource_name_prefixes : "arn:aws:lambda:${local.region}:${local.account_id}:function:${p}*"]
  sqs_arns      = [for p in var.resource_name_prefixes : "arn:aws:sqs:${local.region}:${local.account_id}:${p}*"]
  ddb_arns      = [for p in var.resource_name_prefixes : "arn:aws:dynamodb:${local.region}:${local.account_id}:table/${p}*"]
  ecr_arns      = [for p in var.resource_name_prefixes : "arn:aws:ecr:${local.region}:${local.account_id}:repository/${p}*"]
  secret_arns   = [for p in var.resource_name_prefixes : "arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:${p}*"]
  log_arns      = [for p in var.resource_name_prefixes : "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${p}*"]
  state_objects = [for b in var.state_bucket_arns : "${b}/*"]
}

resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 1 : 0

  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_policy_document" "trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = var.github_subjects
    }
  }
}

resource "aws_iam_role" "deploy" {
  name                 = var.role_name
  description          = "Assumed by GitHub Actions (OIDC) to deploy agentic-workflow and monitor-grow."
  assume_role_policy   = data.aws_iam_policy_document.trust.json
  max_session_duration = var.max_session_duration
}

data "aws_iam_policy_document" "deploy" {
  statement {
    sid       = "TerraformStateObjects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = local.state_objects
  }

  statement {
    sid       = "TerraformStateBucket"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation", "s3:GetBucketVersioning"]
    resources = var.state_bucket_arns
  }

  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid       = "EcrRepositories"
    actions   = ["ecr:*"]
    resources = local.ecr_arns
  }

  statement {
    sid       = "Lambda"
    actions   = ["lambda:*"]
    resources = concat(local.lambda_arns, [for a in local.lambda_arns : "${a}:*"])
  }

  statement {
    sid = "LambdaEventSourceMappings"
    actions = [
      "lambda:CreateEventSourceMapping", "lambda:UpdateEventSourceMapping", "lambda:DeleteEventSourceMapping",
      "lambda:GetEventSourceMapping", "lambda:ListEventSourceMappings",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "Sqs"
    actions   = ["sqs:*"]
    resources = local.sqs_arns
  }

  statement {
    sid       = "DynamoDb"
    actions   = ["dynamodb:*"]
    resources = concat(local.ddb_arns, [for a in local.ddb_arns : "${a}/*"])
  }

  statement {
    sid       = "SecretsManager"
    actions   = ["secretsmanager:*"]
    resources = local.secret_arns
  }

  statement {
    sid       = "CloudWatchLogs"
    actions   = ["logs:*"]
    resources = concat(local.log_arns, [for a in local.log_arns : "${a}:*"])
  }

  statement {
    sid       = "LogsDescribe"
    actions   = ["logs:DescribeLogGroups"]
    resources = ["*"]
  }

  statement {
    sid       = "ApiGateway"
    actions   = ["apigateway:*"]
    resources = ["arn:aws:apigateway:${local.region}::/apis", "arn:aws:apigateway:${local.region}::/apis/*", "arn:aws:apigateway:${local.region}::/tags/*"]
  }

  # Role/policy management is limited to name-prefixed resources to bound privilege escalation.
  statement {
    sid = "IamManagePrefixed"
    actions = [
      "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:UpdateRole", "iam:UpdateAssumeRolePolicy",
      "iam:TagRole", "iam:UntagRole", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:ListInstanceProfilesForRole",
      "iam:PutRolePolicy", "iam:GetRolePolicy", "iam:DeleteRolePolicy",
      "iam:AttachRolePolicy", "iam:DetachRolePolicy",
      "iam:CreatePolicy", "iam:DeletePolicy", "iam:GetPolicy", "iam:GetPolicyVersion", "iam:ListPolicyVersions",
      "iam:CreatePolicyVersion", "iam:DeletePolicyVersion", "iam:TagPolicy", "iam:UntagPolicy",
      "iam:PassRole",
    ]
    resources = concat(local.role_arns, local.policy_arns)
  }

  statement {
    sid       = "IamLocalRunnerUser"
    actions   = ["iam:*User*", "iam:*UserPolicy*"]
    resources = [for p in var.resource_name_prefixes : "arn:aws:iam::${local.account_id}:user/${p}*"]
  }

  statement {
    sid       = "ReadOnlyHelpers"
    actions   = ["sts:GetCallerIdentity", "iam:GetOpenIDConnectProvider", "iam:ListOpenIDConnectProviders"]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "deploy" {
  name        = "${var.role_name}-policy"
  description = "Permissions for GitHub Actions deploys of agentic-workflow and monitor-grow."
  policy      = data.aws_iam_policy_document.deploy.json
}

resource "aws_iam_role_policy_attachment" "deploy" {
  role       = aws_iam_role.deploy.name
  policy_arn = aws_iam_policy.deploy.arn
}

resource "aws_iam_role_policy_attachment" "additional" {
  for_each = toset(var.additional_policy_arns)

  role       = aws_iam_role.deploy.name
  policy_arn = each.value
}
