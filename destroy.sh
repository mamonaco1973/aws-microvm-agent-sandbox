#!/bin/bash
# ================================================================================
# File: destroy.sh
# ================================================================================
#
# Purpose:
#   Tears down the Resume Scoring application stack deployed by apply.sh.
#
#   Destroys all backend infrastructure provisioned by Terraform, including:
#     - Lambda functions, API Gateway, Cognito, DynamoDB, SQS, and S3 buckets.
#
# ================================================================================
# GLOBAL CONFIGURATION
# ================================================================================

# ------------------------------------------------------------------------------
# AWS REGION CONFIGURATION
# ------------------------------------------------------------------------------
# Sets the default AWS region used by:
#   - AWS CLI commands
#   - Terraform providers
# ------------------------------------------------------------------------------
export AWS_DEFAULT_REGION="us-east-1"

# ------------------------------------------------------------------------------
# BEDROCK MODEL CONFIGURATION
# ------------------------------------------------------------------------------
# Terraform needs the model ID during destroy to resolve variable references.
# ------------------------------------------------------------------------------
source "$(dirname "$0")/bedrock-config.sh"

# ------------------------------------------------------------------------------
# STRICT SHELL EXECUTION MODE
# ------------------------------------------------------------------------------
# Enforces defensive Bash behavior:
#   -e  Exit immediately on command failure
#   -u  Treat unset variables as errors
#   -o pipefail  Fail pipelines if any command fails
# ------------------------------------------------------------------------------
set -euo pipefail

# ================================================================================
# CORE INFRASTRUCTURE TEARDOWN
# ================================================================================

# ------------------------------------------------------------------------------
# EMPTY DYNAMODB TABLE
# ------------------------------------------------------------------------------
# Terraform cannot destroy a non-empty DynamoDB table that has deletion
# protection off, but emptying it first makes destroy reliable and avoids
# orphaned data on re-deploy.
# ------------------------------------------------------------------------------
echo "NOTE: Destroying Application Core Services..."

cd 01-core || {
  echo "ERROR: Directory 01-core not found."
  exit 1
}

terraform init
terraform destroy -auto-approve \
  -var="bedrock_model_id=${BEDROCK_MODEL_ID}" \
  -var="google_client_id=${AWS_ASKMIKE_GOOGLE_CLIENT_ID:-}" \
  -var="google_client_secret=${AWS_ASKMIKE_GOOGLE_CLIENT_SECRET:-}" \
  -var="custom_domain=${AWS_ASKMIKE_CUSTOM_DOMAIN:-}"

cd .. || exit 1

# ================================================================================
# COMPLETION
# ================================================================================

# ------------------------------------------------------------------------------
# TEARDOWN COMPLETE
# ------------------------------------------------------------------------------
# Indicates that all Terraform stacks were destroyed successfully.
# ------------------------------------------------------------------------------
echo "NOTE: Infrastructure teardown complete."

# ================================================================================
# END OF SCRIPT
# ================================================================================