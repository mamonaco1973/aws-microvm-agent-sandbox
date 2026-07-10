# ================================================================================
# numpy Lambda layer
# Installed separately because numpy's source-tree guard prevents it from
# loading when placed in the same flat directory as the function handler.
# ================================================================================

resource "aws_lambda_layer_version" "numpy" {
  filename            = data.archive_file.numpy_layer_zip.output_path
  source_code_hash    = data.archive_file.numpy_layer_zip.output_base64sha256
  layer_name          = "numpy-${random_id.bucket_suffix.hex}"
  compatible_runtimes = ["python3.11"]
}

# ================================================================================
# API Lambda function
# Handles all synchronous API Gateway requests
# ================================================================================

resource "aws_lambda_function" "api" {
  function_name = "rag-api-${random_id.bucket_suffix.hex}"

  filename         = data.archive_file.lambdas_zip.output_path
  source_code_hash = data.archive_file.lambdas_zip.output_base64sha256

  handler = "handler.lambda_handler"
  runtime = "python3.11"

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
# SQS-triggered RAG pipeline — embed, retrieve, call Haiku, store result
# ================================================================================

resource "aws_lambda_function" "worker" {
  function_name = "rag-worker-${random_id.bucket_suffix.hex}"

  filename         = data.archive_file.lambdas_zip.output_path
  source_code_hash = data.archive_file.lambdas_zip.output_base64sha256

  handler = "worker.lambda_handler"
  runtime = "python3.11"

  role        = aws_iam_role.lambda_exec.arn
  timeout     = 300
  memory_size = 512
  layers      = [aws_lambda_layer_version.numpy.arn]

  environment {
    variables = {
      TABLE_NAME          = aws_dynamodb_table.app_table.name
      BACKEND_BUCKET_NAME = aws_s3_bucket.backend.bucket
      QUERY_QUEUE_URL     = aws_sqs_queue.query_requests.id
      BEDROCK_MODEL_ID    = var.bedrock_model_id
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
