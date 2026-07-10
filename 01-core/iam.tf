# ================================================================================
# Lambda execution role
# ================================================================================

resource "aws_iam_role" "lambda_exec" {
  name = "rag-app-lambda-${random_id.bucket_suffix.hex}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# ================================================================================
# CloudWatch logging
# ================================================================================

resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# ================================================================================
# DynamoDB access
# ================================================================================

resource "aws_iam_policy" "lambda_dynamodb" {
  name = "rag-app-dynamodb-${random_id.bucket_suffix.hex}"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "dynamodb:PutItem",
        "dynamodb:GetItem",
        "dynamodb:Query",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        # Scan used to count registered users for the USER_CAP check
        "dynamodb:Scan"
      ]
      Resource = aws_dynamodb_table.app_table.arn
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_dynamodb_attach" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = aws_iam_policy.lambda_dynamodb.arn
}

# ================================================================================
# S3 access — user data only (question/answer/trace payloads). No corpus: the
# agent owns retrieval, so there is nothing to read under corpus/ anymore.
# ================================================================================

resource "aws_iam_policy" "lambda_s3" {
  name = "agentops-s3-${random_id.bucket_suffix.hex}"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "BackendBucketList"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.backend.arn
      },
      {
        Sid      = "UserDataAccess"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.backend.arn}/users/*"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_s3_attach" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = aws_iam_policy.lambda_s3.arn
}

# ================================================================================
# SQS access
# ================================================================================

resource "aws_iam_policy" "lambda_sqs" {
  name = "rag-app-sqs-${random_id.bucket_suffix.hex}"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "QueryQueueAccess"
      Effect = "Allow"
      Action = [
        "sqs:GetQueueAttributes",
        "sqs:GetQueueUrl",
        "sqs:SendMessage",
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:ChangeMessageVisibility"
      ]
      Resource = [
        aws_sqs_queue.query_requests.arn,
        aws_sqs_queue.query_requests_dlq.arn
      ]
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_sqs_attach" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = aws_iam_policy.lambda_sqs.arn
}

# ================================================================================
# Bedrock access — the worker invokes the AGENT (not a raw model). InvokeAgent on
# the agent + its alias is all it needs; the agent's own role calls the model.
# ================================================================================

resource "aws_iam_policy" "lambda_bedrock" {
  name = "agentops-bedrock-${random_id.bucket_suffix.hex}"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "InvokeAgent"
      Effect = "Allow"
      Action = ["bedrock:InvokeAgent"]
      Resource = [
        aws_bedrockagent_agent.ops.agent_arn,
        aws_bedrockagent_agent_alias.live.agent_alias_arn,
      ]
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_bedrock_attach" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = aws_iam_policy.lambda_bedrock.arn
}
