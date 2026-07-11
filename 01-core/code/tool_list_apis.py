# ================================================================================
# tool_list_apis.py  — agent tool: list_api_gateways
#
# Read-only. Lists both HTTP/WebSocket APIs (API Gateway v2) and REST APIs
# (v1). IAM: apigateway:GET (covers both /apis and /restapis).
# ================================================================================

import logging

import boto3

from tool_common import respond

logger = logging.getLogger()
logger.setLevel(logging.INFO)

apigw_v2 = boto3.client("apigatewayv2")
apigw_v1 = boto3.client("apigateway")


def lambda_handler(event, context):
    logger.info("list_api_gateways invoked")
    rows = []
    try:
        # HTTP + WebSocket APIs (v2)
        resp = apigw_v2.get_apis(MaxResults="500")
        for api in resp.get("Items", []):
            rows.append(
                f"{api.get('Name', '(unnamed)')} | {api.get('ProtocolType', '?')} | "
                f"id={api.get('ApiId', '?')} | {api.get('ApiEndpoint', '')}"
            )
    except Exception as exc:
        logger.warning("get_apis (v2) failed: %s", exc)

    try:
        # REST APIs (v1)
        paginator = apigw_v1.get_paginator("get_rest_apis")
        for page in paginator.paginate():
            for api in page.get("items", []):
                rows.append(
                    f"{api.get('name', '(unnamed)')} | REST | id={api.get('id', '?')}"
                )
    except Exception as exc:
        logger.warning("get_rest_apis (v1) failed: %s", exc)

    if not rows:
        body = "No API Gateway APIs found in this account/region."
    else:
        body = ("API Gateway APIs — name | type | id | endpoint:\n" + "\n".join(rows))

    return respond(event, body)
