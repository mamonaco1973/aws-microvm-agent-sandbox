# ================================================================================
# API Lambda function
# Handles all synchronous API Gateway requests (conversations, queries, usage)
# ================================================================================

resource "aws_lambda_function" "api" {
  function_name = "agent-api-${random_id.bucket_suffix.hex}"

  filename         = data.archive_file.lambdas_zip.output_path
  source_code_hash = data.archive_file.lambdas_zip.output_base64sha256

  handler = "handler.lambda_handler"
  runtime = "python3.13"

  role    = aws_iam_role.lambda_exec.arn
  timeout = 10

  environment {
    variables = {
      TABLE_NAME          = aws_dynamodb_table.app_table.name
      BACKEND_BUCKET_NAME = aws_s3_bucket.backend.bucket
      QUERY_QUEUE_URL     = aws_sqs_queue.query_requests.id
    }
  }
}

# ================================================================================
# CloudWatch log group for API Lambda
# ================================================================================

resource "aws_cloudwatch_log_group" "api_logs" {
  name              = "/aws/lambda/${aws_lambda_function.api.function_name}"
  retention_in_days = 7
}

# ================================================================================
# Worker Lambda function
# SQS-triggered — hands each question to the Bedrock Agent (invoke_agent),
# captures the answer + reasoning/tool-call trace, and stores the result.
# No numpy/embeddings here: the agent owns retrieval/orchestration/memory.
# ================================================================================

resource "aws_lambda_function" "worker" {
  function_name = "agent-worker-${random_id.bucket_suffix.hex}"

  filename         = data.archive_file.lambdas_zip.output_path
  source_code_hash = data.archive_file.lambdas_zip.output_base64sha256

  handler = "worker.lambda_handler"
  runtime = "python3.13"

  role        = aws_iam_role.lambda_exec.arn
  timeout     = 300
  memory_size = 256

  environment {
    variables = {
      TABLE_NAME          = aws_dynamodb_table.app_table.name
      BACKEND_BUCKET_NAME = aws_s3_bucket.backend.bucket
      QUERY_QUEUE_URL     = aws_sqs_queue.query_requests.id
      AGENT_ID            = aws_bedrockagent_agent.ops.agent_id
      AGENT_ALIAS_ID      = aws_bedrockagent_agent_alias.live.agent_alias_id
    }
  }
}

# ================================================================================
# CloudWatch log group for worker Lambda
# ================================================================================

resource "aws_cloudwatch_log_group" "worker_logs" {
  name              = "/aws/lambda/${aws_lambda_function.worker.function_name}"
  retention_in_days = 7
}
