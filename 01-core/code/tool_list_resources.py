# ================================================================================
# tool_list_resources.py  — agent tool: list_ec2_instances
#
# Read-only. Returns a compact text table of EC2 instances (id, Name, type,
# state) for the agent to reason over. IAM: ec2:DescribeInstances only.
# ================================================================================

import logging

import boto3

from tool_common import respond

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ec2 = boto3.client("ec2")


def _name_of(instance):
    for tag in instance.get("Tags", []):
        if tag.get("Key") == "Name":
            return tag["Value"]
    return "(no name)"


def lambda_handler(event, context):
    logger.info("list_ec2_instances invoked")
    try:
        rows = []
        paginator = ec2.get_paginator("describe_instances")
        for page in paginator.paginate():
            for reservation in page.get("Reservations", []):
                for inst in reservation.get("Instances", []):
                    rows.append(
                        f"{inst['InstanceId']} | {_name_of(inst)} | "
                        f"{inst.get('InstanceType', '?')} | "
                        f"{inst.get('State', {}).get('Name', '?')}"
                    )

        if not rows:
            body = "No EC2 instances found in this account/region."
        else:
            body = "EC2 instances (id | name | type | state):\n" + "\n".join(rows)
    except Exception as exc:
        logger.exception("describe_instances failed")
        body = f"Error listing instances: {exc}"

    return respond(event, body)
