#!/bin/bash
# ==============================================================================
# check_env.sh
# ==============================================================================
# Validates local tooling, AWS credentials, and Bedrock model access before
# apply.sh or destroy.sh are allowed to proceed.
# ==============================================================================

set -u

REGION="${AWS_DEFAULT_REGION:-us-east-1}"

echo "NOTE: Validating that required commands are found in your PATH."

commands=("aws" "terraform" "jq" "pip")

missing=0
for cmd in "${commands[@]}"; do
  if ! command -v "$cmd" > /dev/null 2>&1; then
    echo "ERROR: $cmd is not found in the current PATH."
    missing=1
  else
    echo "NOTE: $cmd is found in the current PATH."
  fi
done

if [ "$missing" -ne 0 ]; then
  echo "ERROR: One or more required commands are missing."
  exit 1
fi

echo "NOTE: Checking AWS cli connection."
if ! aws sts get-caller-identity --query "Account" --output text > /dev/null 2>&1; then
  echo "ERROR: Failed to connect to AWS. Check credentials/environment."
  exit 1
fi
echo "NOTE: Successfully logged into AWS."

# Bedrock model ID is set by apply.sh (single source of truth). The fallback
# here only applies if check_env.sh is run standalone.
BEDROCK_MODEL_ID="${BEDROCK_MODEL_ID:-us.anthropic.claude-haiku-4-5-20251001-v1:0}"

echo "NOTE: Checking Bedrock inference profile ${BEDROCK_MODEL_ID} in ${REGION}."

if ! aws bedrock list-inference-profiles --region "${REGION}" \
       --query "inferenceProfileSummaries[?inferenceProfileId=='${BEDROCK_MODEL_ID}'].inferenceProfileId" \
       --output text 2>/dev/null | grep -q "${BEDROCK_MODEL_ID}"; then
  echo "ERROR: Inference profile ${BEDROCK_MODEL_ID} not available in ${REGION}."
  echo "       Enable access: https://console.aws.amazon.com/bedrock/home?region=${REGION}#/modelaccess"
  exit 1
fi

echo "NOTE: Testing Bedrock model invocation..."
if ! aws bedrock invoke-model \
  --region "${REGION}" \
  --model-id "${BEDROCK_MODEL_ID}" \
  --content-type "application/json" \
  --accept "application/json" \
  --body '{"anthropic_version":"bedrock-2023-05-31","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}' \
  /tmp/bedrock-test-out.json > /dev/null 2>&1; then
    ERR=$(cat /tmp/bedrock-test-out.json 2>/dev/null)
    if echo "$ERR" | grep -q "AccessDeniedException"; then
        echo "ERROR: Bedrock invocation failed — model access not enabled."
        echo "       Enable access: https://console.aws.amazon.com/bedrock/home?region=${REGION}#/modelaccess"
        exit 1
    fi
fi
echo "NOTE: Bedrock invocation access confirmed."
