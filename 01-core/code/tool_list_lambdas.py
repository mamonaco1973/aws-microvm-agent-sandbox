# ================================================================================
# tool_list_lambdas.py  — agent tool: list_lambda_functions
#
# Read-only. Returns the account's Lambda functions with the details an operator
# cares about (runtime, memory, timeout, code size, last modified).
# IAM: lambda:ListFunctions only.
# ================================================================================

import logging

import boto3

from tool_common import respond

logger = logging.getLogger()
logger.setLevel(logging.INFO)

lam = boto3.client("lambda")

_MAX = 200  # safety cap so a huge account can't blow the response size


def lambda_handler(event, context):
    logger.info("list_lambda_functions invoked")
    try:
        rows = []
        paginator = lam.get_paginator("list_functions")
        for page in paginator.paginate():
            for fn in page.get("Functions", []):
                rows.append(
                    f"{fn['FunctionName']} | {fn.get('Runtime', 'container')} | "
                    f"{fn.get('MemorySize', '?')}MB | {fn.get('Timeout', '?')}s | "
                    f"{int(fn.get('CodeSize', 0) or 0) // 1024}KB | "
                    f"modified {fn.get('LastModified', '?')[:10]}"
                )
                if len(rows) >= _MAX:
                    break
            if len(rows) >= _MAX:
                break

        if not rows:
            body = "No Lambda functions found in this account/region."
        else:
            body = (f"Lambda functions ({len(rows)}) — name | runtime | memory | "
                    "timeout | code size | last modified:\n" + "\n".join(rows))
    except Exception as exc:
        logger.exception("list_functions failed")
        body = f"Error listing Lambda functions: {exc}"

    return respond(event, body)
