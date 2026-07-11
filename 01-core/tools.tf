# ================================================================================
# Tool Lambdas — the agent's "hands"
#
# One Lambda per action group. Each runs under its OWN least-privilege role, so
# the agent can only ever do what a given tool's IAM allows — the containment
# story of the demo. All six share the single lambdas.zip (different handlers).
#
# `statements` is each tool's exact AWS permission set. Read tools are read-only;
# control_lambda is the only one that can mutate, and only a function's mem/timeout.
# ================================================================================

locals {
  tools = {
    list_lambdas = {
      handler     = "tool_list_lambdas.lambda_handler"
      description = "List Lambda functions"
      statements = [{
        Effect   = "Allow"
        Action   = ["lambda:ListFunctions"]
        Resource = "*"
      }]
    }
    list_apis = {
      handler     = "tool_list_apis.lambda_handler"
      description = "List API Gateway APIs (HTTP + REST)"
      # apigateway:GET covers both /apis (v2) and /restapis (v1) reads.
      statements = [{
        Effect   = "Allow"
        Action   = ["apigateway:GET"]
        Resource = "*"
      }]
    }
    list_tables = {
      handler     = "tool_list_tables.lambda_handler"
      description = "List DynamoDB tables"
      statements = [{
        Effect   = "Allow"
        Action   = ["dynamodb:ListTables", "dynamodb:DescribeTable"]
        Resource = "*"
      }]
    }
    list_buckets = {
      handler     = "tool_list_buckets.lambda_handler"
      description = "List S3 buckets"
      statements = [{
        Effect   = "Allow"
        Action   = ["s3:ListAllMyBuckets", "s3:GetBucketLocation"]
        Resource = "*"
      }]
    }
    get_costs = {
      handler     = "tool_get_costs.lambda_handler"
      description = "Cost Explorer month-to-date"
      statements = [{
        Effect   = "Allow"
        Action   = ["ce:GetCostAndUsage"]
        Resource = "*"
      }]
    }
    control_lambda = {
      handler     = "tool_control_lambda.lambda_handler"
      description = "Update a Lambda's memory/timeout (the only mutating tool)"
      statements = [{
        Effect   = "Allow"
        Action   = ["lambda:GetFunctionConfiguration", "lambda:UpdateFunctionConfiguration"]
        Resource = "*"
      }]
    }
  }
}

# ------------------------------------------------------------------------------
# Per-tool execution role (logs + the tool's own narrow AWS access)
# ------------------------------------------------------------------------------
resource "aws_iam_role" "tool" {
  for_each = local.tools
  name     = "agent-tool-${each.key}-${random_id.bucket_suffix.hex}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "tool_logs" {
  for_each   = local.tools
  role       = aws_iam_role.tool[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "tool_access" {
  for_each = local.tools
  name     = "tool-access"
  role     = aws_iam_role.tool[each.key].id

  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = each.value.statements
  })
}

# ------------------------------------------------------------------------------
# The tool functions (all share lambdas.zip; each points at its own handler)
# ------------------------------------------------------------------------------
resource "aws_lambda_function" "tool" {
  for_each = local.tools

  function_name = "agent-tool-${each.key}-${random_id.bucket_suffix.hex}"
  description   = each.value.description

  filename         = data.archive_file.lambdas_zip.output_path
  source_code_hash = data.archive_file.lambdas_zip.output_base64sha256

  handler = each.value.handler
  runtime = "python3.13"
  role    = aws_iam_role.tool[each.key].arn
  timeout = 30
}

resource "aws_cloudwatch_log_group" "tool" {
  for_each          = local.tools
  name              = "/aws/lambda/${aws_lambda_function.tool[each.key].function_name}"
  retention_in_days = 7
}

# ------------------------------------------------------------------------------
# Let the Bedrock agent invoke each tool Lambda (resource-based permission,
# scoped to this account's agent). This is how the agent's tool calls actually
# reach the functions.
# ------------------------------------------------------------------------------
resource "aws_lambda_permission" "tool_bedrock" {
  for_each      = local.tools
  statement_id  = "AllowBedrockAgentInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.tool[each.key].function_name
  principal     = "bedrock.amazonaws.com"
  source_arn    = aws_bedrockagent_agent.ops.agent_arn
}
