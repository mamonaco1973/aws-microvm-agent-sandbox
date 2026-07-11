# ================================================================================
# tool_list_tables.py  — agent tool: list_dynamodb_tables
#
# Read-only. Lists DynamoDB tables with item count, size, and billing mode.
# IAM: dynamodb:ListTables + dynamodb:DescribeTable.
# ================================================================================

import logging

import boto3

from tool_common import respond

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ddb = boto3.client("dynamodb")

_MAX = 50  # cap describe calls so a large account stays within the timeout


def lambda_handler(event, context):
    logger.info("list_dynamodb_tables invoked")
    try:
        names = []
        paginator = ddb.get_paginator("list_tables")
        for page in paginator.paginate():
            names.extend(page.get("TableNames", []))

        if not names:
            return respond(event, "No DynamoDB tables found in this account/region.")

        rows = []
        for name in names[:_MAX]:
            try:
                t = ddb.describe_table(TableName=name)["Table"]
                # PAY_PER_REQUEST tables report billing mode here; provisioned omit it.
                billing = (t.get("BillingModeSummary", {}) or {}).get(
                    "BillingMode", "PROVISIONED")
                rows.append(
                    f"{name} | {int(t.get('ItemCount', 0) or 0):,} items | "
                    f"{int(t.get('TableSizeBytes', 0) or 0) // 1024}KB | {billing}"
                )
            except Exception as exc:
                rows.append(f"{name} | (describe failed: {exc})")

        note = "" if len(names) <= _MAX else f"\n(showing first {_MAX} of {len(names)})"
        body = ("DynamoDB tables — name | items | size | billing:\n"
                + "\n".join(rows) + note)
    except Exception as exc:
        logger.exception("list_tables failed")
        body = f"Error listing DynamoDB tables: {exc}"

    return respond(event, body)
