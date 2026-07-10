#!/bin/bash
# ================================================================================
# apply.sh
# Full deployment of the Cloud Ops Copilot (Bedrock Agent) stack.
#
# Workflow:
#   1. Validate environment, AWS credentials, and Bedrock model access
#   2. Deploy backend (Bedrock Agent + tool Lambdas, API/worker Lambdas, API
#      Gateway, Cognito, SQS, DynamoDB, S3, CloudFront)
#   3. Generate config.js for the SPA and upload it to S3
#   4. Print the deployed URL
#
# No corpus/ingest step and no numpy layer: retrieval/orchestration/memory all
# live inside the Bedrock Agent, so there is nothing to embed or index.
# ================================================================================

export AWS_DEFAULT_REGION="us-east-1"

# Exports BEDROCK_MODEL_ID — the model the AGENT reasons with. Sourced before
# strict mode so simple assignments don't trip the unbound-variable check. Also
# used by check_env.sh for the model-access pre-flight.
source "$(dirname "$0")/bedrock-config.sh"

set -euo pipefail

# ================================================================================
# Environment pre-check
# ================================================================================

echo "NOTE: Running environment validation..."
./check_env.sh || { echo "ERROR: Environment validation failed. Exiting."; exit 1; }

# ================================================================================
# Backend deployment
# ================================================================================

echo "NOTE: Deploying backend (agent, tools, API)..."

cd 01-core || { echo "ERROR: 01-core directory missing."; exit 1; }

terraform init
terraform apply -auto-approve \
  -var="agent_foundation_model=${BEDROCK_MODEL_ID}" \
  -var="google_client_id=${AWS_AGENTOPS_GOOGLE_CLIENT_ID:-}" \
  -var="google_client_secret=${AWS_AGENTOPS_GOOGLE_CLIENT_SECRET:-}" \
  -var="custom_domain=${AWS_AGENTOPS_CUSTOM_DOMAIN:-}"

export API_BASE_URL=$(terraform output -raw api_endpoint)
export BUCKET_NAME=$(terraform output -raw frontend_bucket_name)
export COGNITO_DOMAIN=$(terraform output -raw cognito_hosted_ui_base)
export COGNITO_CLIENT_ID=$(terraform output -raw cognito_user_pool_client_id)
export CF_DISTRIBUTION_ID=$(terraform output -raw cloudfront_distribution_id)

cd .. || exit 1

# ================================================================================
# Frontend deployment
# ================================================================================

echo "NOTE: Deploying web application..."

cd 02-webapp || { echo "ERROR: 02-webapp directory missing."; exit 1; }

envsubst < js/config.js.tmpl > js/config.js || {
  echo "ERROR: Failed to generate config.js."
  exit 1
}

aws s3 cp . "s3://${BUCKET_NAME}" --recursive --exclude "*.tmpl"

# Invalidate CloudFront so updated assets are served immediately
aws cloudfront create-invalidation \
  --distribution-id "${CF_DISTRIBUTION_ID}" \
  --paths "/*" > /dev/null

cd ..

# ================================================================================
# Post-deploy validation
# ================================================================================

echo "NOTE: Running post-deployment validation..."
./validate.sh
