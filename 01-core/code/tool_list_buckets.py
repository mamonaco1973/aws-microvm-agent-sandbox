# ================================================================================
# tool_list_buckets.py  — agent tool: list_s3_buckets
#
# Read-only. Lists S3 buckets with region and creation date. (Bucket byte size
# isn't returned — S3 has no cheap size API; that lives in CloudWatch/Storage
# Lens.) IAM: s3:ListAllMyBuckets + s3:GetBucketLocation.
# ================================================================================

import logging

import boto3

from tool_common import respond

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")

_MAX = 200  # cap GetBucketLocation calls


def lambda_handler(event, context):
    logger.info("list_s3_buckets invoked")
    try:
        buckets = s3.list_buckets().get("Buckets", [])
        if not buckets:
            return respond(event, "No S3 buckets found in this account.")

        rows = []
        for b in buckets[:_MAX]:
            name = b["Name"]
            try:
                loc = s3.get_bucket_location(Bucket=name).get("LocationConstraint")
                region = loc or "us-east-1"   # null constraint == us-east-1
            except Exception:
                region = "?"
            created = b.get("CreationDate")
            created_str = created.date().isoformat() if hasattr(created, "date") else "?"
            rows.append(f"{name} | {region} | created {created_str}")

        note = "" if len(buckets) <= _MAX else f"\n(showing first {_MAX} of {len(buckets)})"
        body = "S3 buckets — name | region | created:\n" + "\n".join(rows) + note
    except Exception as exc:
        logger.exception("list_buckets failed")
        body = f"Error listing S3 buckets: {exc}"

    return respond(event, body)
