# ================================================================================
# Provider constraints
#
# The Bedrock Agent resources — and agent memory_configuration in particular —
# need a reasonably recent AWS provider. Pin a floor rather than an exact version
# so `terraform init` still tracks patch/minor updates.
# ================================================================================

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.70"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.5"
    }
  }
}
