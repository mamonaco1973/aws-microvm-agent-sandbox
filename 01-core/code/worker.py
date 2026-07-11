# ================================================================================
# worker.py
#
# SQS-triggered worker. For each query message it hands the question to the
# Bedrock Agent and stores the result:
#   1. Read question.txt from S3
#   2. invoke_agent(sessionId=conv, memoryId=user, inputText=question, trace=on)
#   3. Stream the completion → answer text + a reasoning/tool-call trace
#   4. Write answer.txt + trace.json to S3
#   5. Update the DynamoDB query record + accumulate the agent's token usage
#
# There is no retrieval here: retrieval, prompt assembly, tool orchestration, and
# conversation memory all live inside the agent. The worker is just the async
# bridge between the browser and invoke_agent — same job the old worker did, with
# its middle replaced. sessionId gives the agent short-term (this conversation)
# memory; memoryId gives it long-term memory across conversations.
#
# Expected SQS message body: {"user_id": "...", "conv_id": "...", "query_id": "..."}
# ================================================================================

import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3
from botocore.config import Config

# ================================================================================
# Logging
# ================================================================================

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ================================================================================
# AWS clients
# ================================================================================

dynamodb = boto3.resource("dynamodb")
table    = dynamodb.Table(os.environ["TABLE_NAME"])
s3       = boto3.client("s3")

# Agent runs can chain several model + tool round-trips, so allow a long read.
agent_rt = boto3.client(
    "bedrock-agent-runtime",
    config=Config(read_timeout=240, connect_timeout=10),
)

# ================================================================================
# Environment
# ================================================================================

BACKEND_BUCKET = os.environ["BACKEND_BUCKET_NAME"]
AGENT_ID       = os.environ["AGENT_ID"]
AGENT_ALIAS_ID = os.environ["AGENT_ALIAS_ID"]


# ================================================================================
# Generic helpers
# ================================================================================

def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _read_s3_text(key):
    return s3.get_object(Bucket=BACKEND_BUCKET, Key=key)["Body"].read().decode("utf-8")


def _write_s3_text(key, text):
    s3.put_object(
        Bucket=BACKEND_BUCKET, Key=key,
        Body=text.encode("utf-8"),
        ContentType="text/plain; charset=utf-8",
    )


def _write_s3_json(key, obj):
    s3.put_object(
        Bucket=BACKEND_BUCKET, Key=key,
        Body=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )


def _s3_prefix(user_id, conv_id, query_id):
    return (
        f"users/USER#{user_id}/conversations/"
        f"CONV#{conv_id}/QUERY#{query_id}"
    )


# ================================================================================
# DynamoDB helpers
# ================================================================================

def _update_query_status(user_id, conv_id, query_id, status):
    table.update_item(
        Key={"pk": f"USER#{user_id}", "sk": f"QUERY#{conv_id}#{query_id}"},
        UpdateExpression="SET #s = :s, updated_at = :u",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": status, ":u": utc_now()},
    )


def _finalize_query(user_id, conv_id, query_id, answer_key, trace_key, tokens_used):
    table.update_item(
        Key={"pk": f"USER#{user_id}", "sk": f"QUERY#{conv_id}#{query_id}"},
        UpdateExpression=(
            "SET #s = :s, answer_s3_key = :a, "
            "trace_s3_key = :tr, tokens_used = :t, updated_at = :u"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s":  "complete",
            ":a":  answer_key,
            ":tr": trace_key,
            ":t":  tokens_used,
            ":u":  utc_now(),
        },
    )


def _fail_query(user_id, conv_id, query_id, reason):
    table.update_item(
        Key={"pk": f"USER#{user_id}", "sk": f"QUERY#{conv_id}#{query_id}"},
        UpdateExpression="SET #s = :s, status_message = :m, updated_at = :u",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": "failed",
            ":m": str(reason)[:500],
            ":u": utc_now(),
        },
    )


def accumulate_tokens(user_id, input_tokens, output_tokens):
    """Add the agent's consumed tokens to the user's lifetime usage record."""
    total = int(input_tokens or 0) + int(output_tokens or 0)
    if total <= 0:
        return
    try:
        table.update_item(
            Key={"pk": f"USER#{user_id}", "sk": "USER#USAGE"},
            UpdateExpression="ADD tokens_used :n",
            ExpressionAttributeValues={":n": total},
        )
    except Exception:
        logger.exception("Failed to update token usage for user_id=%s", user_id)


# ================================================================================
# Trace parsing — turn the raw agent trace stream into a compact, UI-friendly
# list of steps (reasoning + tool calls + tool results) and total token usage.
# Best-effort: the trace schema is deep and can change, so every access is
# defensive — a parse miss must never fail the query.
# ================================================================================

def _collect_trace(trace_event, steps, usage):
    """Fold one agent trace event into the running steps list + usage tally."""
    try:
        t = (trace_event or {}).get("trace", {})
        orch = t.get("orchestrationTrace")
        if not orch:
            return

        # The model's reasoning before it acts.
        rationale = (orch.get("rationale") or {}).get("text")
        if rationale:
            steps.append({"type": "reasoning", "text": rationale.strip()})

        # A decision to call a tool (which action group / function + args).
        inv = (orch.get("invocationInput") or {}).get("actionGroupInvocationInput")
        if inv:
            params = {
                p.get("name"): p.get("value")
                for p in (inv.get("parameters") or [])
            }
            steps.append({
                "type":  "tool_call",
                "tool":  inv.get("function") or inv.get("actionGroupName") or "tool",
                "input": params,
            })

        # The result the tool returned.
        obs = orch.get("observation") or {}
        ag_out = obs.get("actionGroupInvocationOutput") or {}
        if ag_out.get("text"):
            steps.append({"type": "tool_result", "text": ag_out["text"][:1500]})

        # Token usage for each model round-trip in the orchestration loop.
        meta = (orch.get("modelInvocationOutput") or {}).get("metadata") or {}
        u = meta.get("usage") or {}
        usage["input"]  += int(u.get("inputTokens") or 0)
        usage["output"] += int(u.get("outputTokens") or 0)
    except Exception:
        logger.exception("Trace parse error (non-fatal)")


# ================================================================================
# Core: hand the question to the agent
# ================================================================================

def process_query(user_id, conv_id, query_id):
    """Invoke the Bedrock Agent for one query and persist answer + trace."""

    _update_query_status(user_id, conv_id, query_id, "processing")
    prefix = _s3_prefix(user_id, conv_id, query_id)

    try:
        question = _read_s3_text(f"{prefix}/question.txt").strip()
    except Exception as exc:
        logger.exception("Failed to read question from S3")
        _fail_query(user_id, conv_id, query_id, f"Could not read question: {exc}")
        return

    if not question:
        _fail_query(user_id, conv_id, query_id, "Question is empty")
        return

    # --------------------------------------------------------------------------
    # invoke_agent — sessionId = this conversation (short-term memory),
    # memoryId = this user (long-term memory across conversations). enableTrace
    # gives us the reasoning/tool-call stream to show in the UI.
    # --------------------------------------------------------------------------
    answer_parts = []
    trace_steps  = []
    usage        = {"input": 0, "output": 0}
    t0 = time.time()

    try:
        response = agent_rt.invoke_agent(
            agentId=AGENT_ID,
            agentAliasId=AGENT_ALIAS_ID,
            sessionId=conv_id,
            memoryId=user_id,
            inputText=question,
            enableTrace=True,
        )
        for event in response.get("completion", []):
            if "chunk" in event:
                answer_parts.append(event["chunk"]["bytes"].decode("utf-8"))
            elif "trace" in event:
                _collect_trace(event["trace"], trace_steps, usage)
    except Exception as exc:
        logger.exception("invoke_agent failed")
        _fail_query(user_id, conv_id, query_id, f"Agent call failed: {exc}")
        return

    answer = "".join(answer_parts).strip() or "(the agent returned no answer)"
    trace_steps.append({"type": "answer"})   # marks the end of the trace for the UI

    logger.info(
        "Agent complete. conv=%s query=%s steps=%d elapsed=%.1fs in=%d out=%d",
        conv_id, query_id, len(trace_steps), time.time() - t0,
        usage["input"], usage["output"],
    )

    # --------------------------------------------------------------------------
    # Persist + finalize
    # --------------------------------------------------------------------------
    answer_key = f"{prefix}/answer.txt"
    trace_key  = f"{prefix}/trace.json"
    try:
        _write_s3_text(answer_key, answer)
        _write_s3_json(trace_key, trace_steps)
    except Exception as exc:
        logger.exception("Failed to write answer/trace to S3")
        _fail_query(user_id, conv_id, query_id, f"Failed to store result: {exc}")
        return

    total_tokens = usage["input"] + usage["output"]
    _finalize_query(user_id, conv_id, query_id, answer_key, trace_key, total_tokens)
    accumulate_tokens(user_id, usage["input"], usage["output"])


# ================================================================================
# Lambda entry point
# ================================================================================

def lambda_handler(event, context):
    """SQS-triggered entry point; each record processed independently."""
    for record in event.get("Records", []):
        try:
            message  = json.loads(record["body"])
            user_id  = str(message.get("user_id",  "")).strip()
            conv_id  = str(message.get("conv_id",  "")).strip()
            query_id = str(message.get("query_id", "")).strip()

            if not user_id or not conv_id or not query_id:
                logger.error("Message missing required fields: %s", message)
                continue

            logger.info("Processing query. user=%s conv=%s query=%s",
                        user_id, conv_id, query_id)
            process_query(user_id, conv_id, query_id)
        except Exception:
            logger.exception("Unhandled error processing SQS record")

    return {"statusCode": 200}
