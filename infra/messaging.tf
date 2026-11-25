resource "aws_sqs_queue" "tasks_dlq" {
  name                        = "${local.prefix}-tasks-dlq.fifo"
  fifo_queue                  = true
  content_based_deduplication = false
  message_retention_seconds   = 1209600 # 14 days
  sqs_managed_sse_enabled     = true
}

resource "aws_sqs_queue" "tasks" {
  name                        = "${local.prefix}-tasks.fifo"
  fifo_queue                  = true
  content_based_deduplication = false # Explicit MessageDeduplicationId from X-GitHub-Delivery
  deduplication_scope         = "messageGroup"
  fifo_throughput_limit       = "perMessageGroupId"
  visibility_timeout_seconds  = var.sqs_visibility_timeout_seconds
  receive_wait_time_seconds   = 20     # Long polling to minimize empty receives
  message_retention_seconds   = 345600 # 4 days
  sqs_managed_sse_enabled     = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.tasks_dlq.arn
    maxReceiveCount     = var.sqs_max_receive_count
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "tasks_dlq" {
  queue_url = aws_sqs_queue.tasks_dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.tasks.arn]
  })
}
