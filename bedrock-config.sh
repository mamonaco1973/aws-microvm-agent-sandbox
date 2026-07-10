# ==============================================================================
# bedrock-config.sh
# ==============================================================================
# Single source of truth for Bedrock model selection. Sourced by apply.sh
# and destroy.sh so both stay in sync.
#
# BEDROCK_MODEL_ID is a cross-region inference profile ID. The us.* prefix
# routes requests across us-east-1, us-east-2, and us-west-2 automatically.
#
# To switch models, edit this value. It flows to:
#   • check_env.sh  — pre-flight profile check + invoke probe (via exported env)
#   • 01-core       — worker Lambda BEDROCK_MODEL_ID env var (via Terraform var)
# ==============================================================================

export BEDROCK_MODEL_ID="us.anthropic.claude-haiku-4-5-20251001-v1:0"
