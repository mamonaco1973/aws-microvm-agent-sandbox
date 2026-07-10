# ================================================================================
# tool_get_alarms.py  — agent tool: get_cloudwatch_alarms
#
# Read-only. Returns CloudWatch alarms and their current state. IAM:
# cloudwatch:DescribeAlarms only.
# ================================================================================

import logging

import boto3

from tool_common import respond

logger = logging.getLogger()
logger.setLevel(logging.INFO)

cw = boto3.client("cloudwatch")


def lambda_handler(event, context):
    logger.info("get_cloudwatch_alarms invoked")
    try:
        rows = []
        paginator = cw.get_paginator("describe_alarms")
        for page in paginator.paginate(AlarmTypes=["MetricAlarm", "CompositeAlarm"]):
            for alarm in page.get("MetricAlarms", []) + page.get("CompositeAlarms", []):
                rows.append(f"{alarm['AlarmName']} → {alarm.get('StateValue', '?')}")

        if not rows:
            body = "No CloudWatch alarms are configured in this account/region."
        else:
            # Surface anything in ALARM first — that's what an operator cares about.
            rows.sort(key=lambda r: (" ALARM" not in f" {r.split('→ ')[-1]}", r))
            body = "CloudWatch alarms (name → state):\n" + "\n".join(rows)
    except Exception as exc:
        logger.exception("describe_alarms failed")
        body = f"Error retrieving alarms: {exc}"

    return respond(event, body)
