# --------------------------------------------------------------------------------
# DATA: archive_file.lambdas_zip
#
# Packages all Lambda source from code/ into one ZIP. The API Lambda, the SQS
# worker, and the four tool Lambdas all share this archive — each function just
# points at its own handler (handler.py / worker.py / tool_*.py).
# --------------------------------------------------------------------------------
data "archive_file" "lambdas_zip" {
  type        = "zip"
  source_dir  = "${path.module}/code"
  output_path = "${path.module}/lambdas.zip"
}
