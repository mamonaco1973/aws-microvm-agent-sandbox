# ================================================================================
# Terraform Remote State — S3 backend
# State is stored in a pre-existing build bucket, not managed by this stack.
# ================================================================================

# terraform {
#   backend "s3" {
#     bucket = "resume-app-824622998597-build"
#     key    = "terraform/state/aws-rag-demo/terraform.tfstate"
#     region = "us-east-1"
#   }
# }
