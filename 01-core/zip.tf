# --------------------------------------------------------------------------------
# DATA: archive_file.lambdas_zip
# --------------------------------------------------------------------------------
# Description:
#   Packages Lambda source code from the local "code" directory
#   into a ZIP archive for deployment.
#
# Expected code layout:
#   code/
#     get.py
#     list.py
#     create.py
#     update.py
#     delete.py
# --------------------------------------------------------------------------------
data "archive_file" "lambdas_zip" {
  type        = "zip"
  source_dir  = "${path.module}/code"
  output_path = "${path.module}/lambdas.zip"
}

# numpy must be in a layer — importing it from the flat function directory
# triggers numpy's source-tree detection check and causes ImportModuleError
data "archive_file" "numpy_layer_zip" {
  type        = "zip"
  source_dir  = "${path.module}/layer"
  output_path = "${path.module}/numpy_layer.zip"
}