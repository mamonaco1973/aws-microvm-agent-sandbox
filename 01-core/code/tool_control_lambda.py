# ================================================================================
# tool_control_lambda.py  — agent tool: update_lambda_config
#
# The ONLY mutating tool. Changes a function's memory size and/or timeout — a
# reversible config tweak. The agent's instructions require it to confirm with
# the user first, but the real containment is IAM: this Lambda's role can only
# UpdateFunctionConfiguration/GetFunction — nothing else.
# ================================================================================

import logging

import boto3

from tool_common import parse_params, respond

logger = logging.getLogger()
logger.setLevel(logging.INFO)

lam = boto3.client("lambda")

_MIN_MEM, _MAX_MEM = 128, 10240      # Lambda memory bounds (MB)
_MIN_TO, _MAX_TO   = 1, 900          # Lambda timeout bounds (s)


def _as_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def lambda_handler(event, context):
    params    = parse_params(event)
    fn_name   = (params.get("function_name") or "").strip()
    mem       = _as_int(params.get("memory_size"))
    timeout   = _as_int(params.get("timeout"))
    logger.info("update_lambda_config: %s mem=%s timeout=%s", fn_name, mem, timeout)

    if not fn_name:
        return respond(event, "No function_name was provided.")
    if mem is None and timeout is None:
        return respond(event, "Nothing to change — provide memory_size and/or timeout.")
    if mem is not None and not (_MIN_MEM <= mem <= _MAX_MEM):
        return respond(event, f"memory_size must be between {_MIN_MEM} and {_MAX_MEM} MB.")
    if timeout is not None and not (_MIN_TO <= timeout <= _MAX_TO):
        return respond(event, f"timeout must be between {_MIN_TO} and {_MAX_TO} seconds.")

    try:
        cur = lam.get_function_configuration(FunctionName=fn_name)
        old_mem, old_to = cur.get("MemorySize"), cur.get("Timeout")

        update = {"FunctionName": fn_name}
        if mem is not None:
            update["MemorySize"] = mem
        if timeout is not None:
            update["Timeout"] = timeout
        lam.update_function_configuration(**update)

        changes = []
        if mem is not None:
            changes.append(f"memory {old_mem}MB → {mem}MB")
        if timeout is not None:
            changes.append(f"timeout {old_to}s → {timeout}s")
        body = f"Updated {fn_name}: " + ", ".join(changes) + "."
    except lam.exceptions.ResourceNotFoundException:
        body = f"No Lambda function named '{fn_name}' in this account/region."
    except Exception as exc:
        logger.exception("update_function_configuration failed")
        body = f"Error updating {fn_name}: {exc}"

    return respond(event, body)
