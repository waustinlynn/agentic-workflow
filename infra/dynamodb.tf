resource "aws_dynamodb_table" "run_state" {
  name         = "${local.prefix}-run-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = var.enable_dynamodb_pitr
  }
}

resource "aws_dynamodb_table" "repo_registry" {
  name         = "${local.prefix}-repo-registry"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "repo_full_name"

  attribute {
    name = "repo_full_name"
    type = "S"
  }

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = var.enable_dynamodb_pitr
  }
}

resource "aws_dynamodb_table_item" "initial_repos" {
  for_each = var.initial_repos

  table_name = aws_dynamodb_table.repo_registry.name
  hash_key   = aws_dynamodb_table.repo_registry.hash_key

  item = jsonencode({
    repo_full_name   = { S = each.key }
    message_group_id = { S = each.value.message_group_id }
    execution_target = { S = each.value.execution_target }
    runtime          = { S = each.value.runtime }
    enabled          = { BOOL = each.value.enabled }
  })
}
