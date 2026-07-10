#!/bin/bash
# ==============================================================================
# validate.sh
# ==============================================================================
# Prints the app URL, API endpoint, and Cognito config after apply.sh completes.
# ==============================================================================

export AWS_DEFAULT_REGION="us-east-1"
set -euo pipefail

CUSTOM_URL=$(terraform -chdir=01-core output -raw custom_domain_url     2>/dev/null || true)
API_BASE=$(terraform -chdir=01-core output -raw api_endpoint            2>/dev/null || true)
COGNITO_UI=$(terraform -chdir=01-core output -raw cognito_hosted_ui_base 2>/dev/null || true)

if [ -z "${CUSTOM_URL}" ] || [ -z "${API_BASE}" ]; then
  echo "ERROR: Could not read Terraform outputs. Run ./apply.sh first."
  exit 1
fi

echo ""
echo "================================================================================="
echo "  Ask Mike — Deployment validated!"
echo "================================================================================="
echo "  App : ${CUSTOM_URL}"
echo "================================================================================="
echo ""
echo "  Google IDP — Authorized redirect URI:"
echo "  ${COGNITO_UI}/oauth2/idpresponse"
echo ""