# ================================================================================
# tool_control_instance.py  — agent tool: control_ec2_instance
#
# The ONLY mutating tool. Starts or stops one EC2 instance. The agent's
# instructions require it to confirm with the user before calling this, but the
# containment is IAM: this Lambda's role can only Start/Stop/Describe EC2 — the
# agent can never exceed what this tool is permitted to do.
# ================================================================================

import logging

import boto3

from tool_common import parse_params, respond

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ec2 = boto3.client("ec2")


def _name_of(instance_id):
    try:
        resp = ec2.describe_instances(InstanceIds=[instance_id])
        tags = resp["Reservations"][0]["Instances"][0].get("Tags", [])
        for tag in tags:
            if tag.get("Key") == "Name":
                return tag["Value"]
    except Exception:
        pass
    return "(no name)"


def lambda_handler(event, context):
    params      = parse_params(event)
    instance_id = (params.get("instance_id") or "").strip()
    action      = (params.get("action") or "").strip().lower()
    logger.info("control_ec2_instance invoked: %s %s", action, instance_id)

    if not instance_id:
        return respond(event, "No instance_id was provided.")
    if action not in ("start", "stop"):
        return respond(event, f"Unsupported action '{action}'. Use 'start' or 'stop'.")

    name = _name_of(instance_id)
    try:
        if action == "start":
            ec2.start_instances(InstanceIds=[instance_id])
            body = f"Started instance {instance_id} ({name}). It is now booting."
        else:
            ec2.stop_instances(InstanceIds=[instance_id])
            body = f"Stopped instance {instance_id} ({name}). It is shutting down."
    except Exception as exc:
        logger.exception("%s_instances failed", action)
        body = f"Error trying to {action} {instance_id}: {exc}"

    return respond(event, body)
