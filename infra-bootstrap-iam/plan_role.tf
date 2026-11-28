# Read-only role for `terraform plan` on pull requests. PR code can assume this role, so it
# intentionally grants only describe/list/get on name-prefixed resources: no write actions,
# no secret values, and no read access to arbitrary S3 objects.
locals {
  create_plan_role = length(var.github_plan_subjects) > 0
}

data "aws_iam_policy_document" "plan_trust" {
  count = local.create_plan_role ? 1 : 0

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
      values   = var.github_plan_subjects
    }
  }
}

data "aws_iam_policy_document" "plan" {
  count = local.create_plan_role ? 1 : 0

  statement {
    sid       = "StateRead"
    actions   = ["s3:GetObject"]
    resources = local.state_objects
  }

  statement {
    sid       = "StateList"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation", "s3:GetBucketVersioning"]
    resources = var.state_bucket_arns
  }

  statement {
    sid       = "IdentityAndLists"
    actions   = ["sts:GetCallerIdentity", "logs:DescribeLogGroups", "lambda:ListEventSourceMappings", "lambda:GetEventSourceMapping", "iam:ListOpenIDConnectProviders"]
    resources = ["*"]
  }

  statement {
    sid       = "EcrRead"
    actions   = ["ecr:Describe*", "ecr:Get*", "ecr:List*"]
    resources = local.ecr_arns
  }

  statement {
    sid       = "LambdaRead"
    actions   = ["lambda:Get*", "lambda:List*"]
    resources = concat(local.lambda_arns, [for a in local.lambda_arns : "${a}:*"])
  }

  statement {
    sid       = "SqsRead"
    actions   = ["sqs:Get*", "sqs:List*"]
    resources = local.sqs_arns
  }

  statement {
    sid       = "DynamoDbRead"
    actions   = ["dynamodb:Describe*", "dynamodb:List*", "dynamodb:GetItem"]
    resources = concat(local.ddb_arns, [for a in local.ddb_arns : "${a}/*"])
  }

  # Metadata only: GetSecretValue is deliberately not granted.
  statement {
    sid       = "SecretsMetadataRead"
    actions   = ["secretsmanager:DescribeSecret", "secretsmanager:GetResourcePolicy"]
    resources = local.secret_arns
  }

  statement {
    sid       = "LogsRead"
    actions   = ["logs:ListTagsForResource", "logs:ListTagsLogGroup"]
    resources = concat(local.log_arns, [for a in local.log_arns : "${a}:*"])
  }

  statement {
    sid       = "ApiGatewayRead"
    actions   = ["apigateway:GET"]
    resources = ["arn:aws:apigateway:${local.region}::/apis", "arn:aws:apigateway:${local.region}::/apis/*", "arn:aws:apigateway:${local.region}::/tags/*"]
  }

  statement {
    sid = "IamRead"
    actions = [
      "iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies",
      "iam:GetPolicy", "iam:GetPolicyVersion", "iam:ListPolicyVersions",
      "iam:GetUser", "iam:ListAttachedUserPolicies", "iam:ListUserPolicies", "iam:ListGroupsForUser", "iam:GetUserPolicy",
    ]
    resources = concat(local.role_arns, local.policy_arns, [for p in var.resource_name_prefixes : "arn:aws:iam::${local.account_id}:user/${p}*"])
  }
}

resource "aws_iam_role" "plan" {
  count = local.create_plan_role ? 1 : 0

  name                 = var.plan_role_name
  description          = "Read-only role assumed by pull_request workflows to run terraform plan."
  assume_role_policy   = data.aws_iam_policy_document.plan_trust[0].json
  max_session_duration = 3600
}

resource "aws_iam_policy" "plan" {
  count = local.create_plan_role ? 1 : 0

  name   = "${var.plan_role_name}-policy"
  policy = data.aws_iam_policy_document.plan[0].json
}

resource "aws_iam_role_policy_attachment" "plan" {
  count = local.create_plan_role ? 1 : 0

  role       = aws_iam_role.plan[0].name
  policy_arn = aws_iam_policy.plan[0].arn
}
