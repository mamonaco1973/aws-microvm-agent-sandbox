# ================================================================================
# Bedrock Agent — the "Cloud Ops Copilot" brain
#
# The agent is the orchestrator: given a question + a session, it decides which
# tools (action groups) to call, in what order, calls them, and answers. Session
# + long-term memory are managed by Bedrock (not by us) — that is the whole point
# of this demo vs. the stateless worker it was forked from.
# ================================================================================

# ------------------------------------------------------------------------------
# Agent service role — the identity Bedrock assumes to run the agent. Needs only
# to invoke the foundation model; it does NOT need the tool Lambdas' permissions
# (each tool Lambda runs under its own least-privilege role).
# ------------------------------------------------------------------------------
resource "aws_iam_role" "agent" {
  name = "agent-role-${random_id.bucket_suffix.hex}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock.amazonaws.com" }
      Action    = "sts:AssumeRole"
      # Confused-deputy guards — only THIS account's agents may assume the role.
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:agent/*" }
      }
    }]
  })
}

resource "aws_iam_role_policy" "agent_invoke_model" {
  name = "invoke-model"
  role = aws_iam_role.agent.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
      # Wildcard the foundation-model region so cross-region inference profiles
      # (us.anthropic.*) resolve to any regional copy of the underlying model.
      Resource = [
        "arn:aws:bedrock:*::foundation-model/*",
        "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:inference-profile/*",
      ]
    }]
  })
}

# ------------------------------------------------------------------------------
# The agent itself. `instruction` is its system prompt; `memory_configuration`
# turns on cross-session long-term memory (session summaries retained 30 days) —
# the capability this whole demo exists to show.
# ------------------------------------------------------------------------------
resource "aws_bedrockagent_agent" "ops" {
  agent_name                  = "agent-copilot-${random_id.bucket_suffix.hex}"
  agent_resource_role_arn     = aws_iam_role.agent.arn
  foundation_model            = var.agent_foundation_model
  idle_session_ttl_in_seconds = 1800
  prepare_agent               = true

  instruction = <<-EOT
    You are Cloud Ops Copilot, an assistant that helps engineers inspect and
    operate their AWS account using the tools provided. Answer with real data
    from the tools — never guess at instance states, costs, or alarm status. If
    a question needs live data, call the appropriate tool rather than
    speculating. When the user asks to START or STOP an instance, ALWAYS restate
    the exact instance id and name and ask them to confirm before calling the
    control tool. Keep answers concise. Remember useful facts the user tells you
    across the conversation — budgets, thresholds, or which instance is "the dev
    box" — and apply them in later questions.
  EOT

  memory_configuration {
    enabled_memory_types = ["SESSION_SUMMARY"]
    storage_days         = 30
  }
}

# ------------------------------------------------------------------------------
# Action groups — one per tool Lambda (defined in tools.tf). One group per tool
# keeps each capability behind its own narrowly-scoped IAM role, and makes the
# agent's decisions legible in the trace ("called get_costs, then list_resources").
# The function_schema is how the agent knows each tool's name, purpose, and args.
# ------------------------------------------------------------------------------
resource "aws_bedrockagent_agent_action_group" "list_resources" {
  agent_id                   = aws_bedrockagent_agent.ops.agent_id
  agent_version              = "DRAFT"
  action_group_name          = "list_resources"
  skip_resource_in_use_check = true

  action_group_executor {
    lambda = aws_lambda_function.tool["list_resources"].arn
  }

  function_schema {
    member_functions {
      functions {
        name        = "list_ec2_instances"
        description = "List EC2 instances in the account with their id, Name tag, type, and current state (running/stopped/etc)."
      }
    }
  }
}

resource "aws_bedrockagent_agent_action_group" "get_costs" {
  agent_id                   = aws_bedrockagent_agent.ops.agent_id
  agent_version              = "DRAFT"
  action_group_name          = "get_costs"
  skip_resource_in_use_check = true

  action_group_executor {
    lambda = aws_lambda_function.tool["get_costs"].arn
  }

  function_schema {
    member_functions {
      functions {
        name        = "get_month_to_date_cost"
        description = "Return this account's month-to-date AWS spend in USD, optionally broken down by service."
        parameters {
          map_block_key = "group_by_service"
          type          = "boolean"
          description   = "If true, break the cost down per AWS service. Default false (total only)."
          required      = false
        }
      }
    }
  }
}

resource "aws_bedrockagent_agent_action_group" "get_alarms" {
  agent_id                   = aws_bedrockagent_agent.ops.agent_id
  agent_version              = "DRAFT"
  action_group_name          = "get_alarms"
  skip_resource_in_use_check = true

  action_group_executor {
    lambda = aws_lambda_function.tool["get_alarms"].arn
  }

  function_schema {
    member_functions {
      functions {
        name        = "get_cloudwatch_alarms"
        description = "List CloudWatch alarms and their current state (OK, ALARM, or INSUFFICIENT_DATA)."
      }
    }
  }
}

resource "aws_bedrockagent_agent_action_group" "control_instance" {
  agent_id                   = aws_bedrockagent_agent.ops.agent_id
  agent_version              = "DRAFT"
  action_group_name          = "control_instance"
  skip_resource_in_use_check = true

  action_group_executor {
    lambda = aws_lambda_function.tool["control_instance"].arn
  }

  function_schema {
    member_functions {
      functions {
        name        = "control_ec2_instance"
        description = "Start or stop a specific EC2 instance. Only call after the user has confirmed the exact instance."
        parameters {
          map_block_key = "instance_id"
          type          = "string"
          description   = "The EC2 instance id, e.g. i-0abc123."
          required      = true
        }
        parameters {
          map_block_key = "action"
          type          = "string"
          description   = "Either 'start' or 'stop'."
          required      = true
        }
      }
    }
  }
}

# ------------------------------------------------------------------------------
# Alias — the stable, invokable endpoint the worker calls. Depends on every
# action group so the auto-"prepare" bakes the tools into the version this alias
# points at (an alias created before the tools would ship an agent with none).
# ------------------------------------------------------------------------------
resource "aws_bedrockagent_agent_alias" "live" {
  agent_alias_name = "live"
  agent_id         = aws_bedrockagent_agent.ops.agent_id

  depends_on = [
    aws_bedrockagent_agent_action_group.list_resources,
    aws_bedrockagent_agent_action_group.get_costs,
    aws_bedrockagent_agent_action_group.get_alarms,
    aws_bedrockagent_agent_action_group.control_instance,
  ]
}
