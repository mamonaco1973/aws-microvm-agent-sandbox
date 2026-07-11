# ================================================================================
# Frontend S3 bucket base name
# ================================================================================

variable "frontend_bucket_base_name" {
  description = "Base name for the frontend S3 bucket"
  type        = string
  default     = "agent-app"
}

# ================================================================================
# Backend S3 bucket base name
# ================================================================================

variable "backend_bucket_base_name" {
  description = "Base name for the backend S3 bucket"
  type        = string
  default     = "agent-data"
}

# ================================================================================
# AWS region
# ================================================================================

variable "region" {
  description = "AWS region for deployment"
  type        = string
  default     = "us-east-1"
}

# ================================================================================
# Agent foundation model — the model the Bedrock Agent reasons with. Must be a
# model/inference-profile that supports Bedrock Agents in this region. Change via
# tfvars if the default isn't enabled for your account.
# ================================================================================

variable "agent_foundation_model" {
  description = "Foundation model (or inference-profile id) the Bedrock Agent uses"
  type        = string
  default     = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
}

# ================================================================================
# Google OAuth — optional; set to enable Google sign-in via Cognito IdP
# Populated from AWS_AGENTOPS_GOOGLE_CLIENT_ID / AWS_AGENTOPS_GOOGLE_CLIENT_SECRET
# ================================================================================

variable "google_client_id" {
  description = "Google OAuth client ID for Cognito identity provider"
  type        = string
  default     = ""
}

variable "google_client_secret" {
  description = "Google OAuth client secret for Cognito identity provider"
  type        = string
  default     = ""
  sensitive   = true
}

# ================================================================================
# Custom domain — leave empty to serve directly from CloudFront's default domain
# When set, also set route53_zone_id to create ACM + DNS records automatically
# ================================================================================

variable "custom_domain" {
  description = "Custom domain name (e.g. copilot.example.com). Leave empty to use CloudFront default domain. The parent hosted zone is looked up automatically."
  type        = string
  default     = ""
}
