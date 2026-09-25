# ================================================================================
# worker.py
#
# SQS-triggered worker. For each query it runs a Bedrock Converse tool loop
# whose tools execute inside this conversation's MicroVM sandbox:
#   1. Read question.txt from S3, plus recent exchanges for context
#   2. converse() with three tools -- run_code, get_result, show_file
#   3. Execute each requested tool against the sandbox (sandbox.py launches it
#      on first use), feed the results back, repeat until the model answers
#   4. Write answer.txt + trace.json to S3, stage shown files as artifacts,
#      mark the query complete and add the tokens to the user's usage
#
# The loop is ours rather than a managed agent's for two reasons that are the
# point of the demo. show_file returns the rendered PNG as an IMAGE block, so
# the model sees what it drew and can fix it -- Bedrock Agents action groups
# return text only. And run_code waits for the cell here, in a Lambda with a
# 15-minute budget, so the model sees a plain result instead of having to
# poll a job id from inside its own reasoning.
#
# The trace is persisted after every step, so the browser can show progress
# ("launched sandbox", "running code") while the query is still processing.
#
# Expected SQS message body: {"user_id": "...", "conv_id": "...", "query_id": "..."}
# ================================================================================

import json
import logging
import os
import re
import struct
import time
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config

import sandbox

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

# A single model turn can write a long cell; allow a long read.
bedrock = boto3.client(
    "bedrock-runtime",
    config=Config(read_timeout=300, connect_timeout=10,
                  retries={"mode": "standard", "max_attempts": 4}),
)

# ================================================================================
# Environment and limits
# ================================================================================

BACKEND_BUCKET = os.environ["BACKEND_BUCKET_NAME"]
MODEL_ID       = os.environ["BEDROCK_MODEL_ID"]

# Model round-trips per query. A fractal takes three or four; the cap stops a
# model that keeps "fixing" the same error from burning the token budget.
MAX_TURNS = 25

# Prior exchanges replayed as context. Text only -- earlier images are not
# resent, since the sandbox itself still holds the files and the variables.
HISTORY_WINDOW = 5

# How long run_code / get_result wait for a cell before handing back the job
# id. Long enough for a pip install; short enough to leave room for more turns.
TOOL_WAIT_SECONDS = 240

# Time kept back from the Lambda deadline to write the answer and finalize.
RESERVE_MS = 60_000

# Cell output handed to the model. The kernel already caps at 64 KB.
TOOL_TEXT_LIMIT = 20_000

# Converse image limits: 3.75 MB and 8000 px a side.
IMAGE_BYTES_LIMIT = 3_750_000
IMAGE_PX_LIMIT    = 8000
IMAGE_FORMATS = {"image/png": "png", "image/jpeg": "jpeg",
                 "image/gif": "gif", "image/webp": "webp"}

SYSTEM_PROMPT = """You are a coding agent with a private Python sandbox: a Lambda \
MicroVM (Firecracker VM, its own kernel) that belongs to this conversation.

How the sandbox works:
- run_code executes Python in ONE persistent session. Variables, functions, \
imports and files survive between calls and between messages in this \
conversation. The working directory is /workspace.
- numpy, matplotlib (Agg backend) and pillow are installed and already loaded \
in memory, so importing them is instant. For anything else, install it from a \
cell: subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pkg"]). \
The sandbox has internet access.
- The sandbox launches automatically the first time you run code. Never ask \
the user to start it.
- A failing cell returns its traceback and the session survives. Read the \
error, fix the code, run it again.

Showing results:
- Save figures to files with plt.savefig(...) and plt.close(); never \
plt.show(). Then call show_file with the path. The image is attached to your \
answer for the user, and returned to you so you can check it.
- Review every image you show before answering. Does it look like what was \
asked for? Is anything floating, clipped, cropped, overlapping, or in large \
empty space? Is all text rendered (no missing-glyph boxes)? If anything is \
off, fix the code, save to the SAME path, and call show_file again -- that \
replaces the earlier attachment, so the user only sees the final version.
- Treat warnings in cell output as bugs to fix. The fonts have no emoji: \
keep emoji out of plot titles and labels.
- Prefer a clean, faithful rendering of what was asked over decoration.
- Never paste file contents, base64, links, or markdown image syntax into \
your answer -- the user already sees every file you showed.

Your final answer: say briefly what you built and the key parameters, then \
include the final code in a ```python block unless it is very long."""

TOOLS = [
    {"toolSpec": {
        "name": "run_code",
        "description": (
            "Run Python in the persistent sandbox session and return its output "
            "(stdout, stderr and, like a REPL, the value of a trailing "
            "expression). State persists across calls. If the cell is still "
            "running after a few minutes this returns a job id instead; call "
            "get_result with it to keep waiting."),
        "inputSchema": {"json": {
            "type": "object",
            "properties": {"code": {"type": "string",
                                    "description": "Python source to execute."}},
            "required": ["code"]}}}},
    {"toolSpec": {
        "name": "get_result",
        "description": (
            "Keep waiting for a cell that run_code reported as still running. "
            "Only needed for long cells such as big installs."),
        "inputSchema": {"json": {
            "type": "object",
            "properties": {"job": {"type": "string",
                                   "description": "Job id from run_code."}},
            "required": ["job"]}}}},
    {"toolSpec": {
        "name": "show_file",
        "description": (
            "Attach a file from the sandbox to your answer so the user sees it "
            "(images render inline; other files become downloads). Images are "
            "also returned to you so you can check the result. Showing the "
            "same path again replaces its earlier attachment."),
        "inputSchema": {"json": {
            "type": "object",
            "properties": {"path": {"type": "string",
                                    "description": "Path in the sandbox, e.g. fractal.png"}},
            "required": ["path"]}}}},
]


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


def _clip(text, limit=TOOL_TEXT_LIMIT):
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [{len(text) - limit} more characters truncated]"


# ================================================================================
# DynamoDB helpers
# ================================================================================

def _query_key(user_id, conv_id, query_id):
    return {"pk": f"USER#{user_id}", "sk": f"QUERY#{conv_id}#{query_id}"}


def _update_query_status(user_id, conv_id, query_id, status, trace_key):
    # trace_key is set up front so polls can read the progress trace while
    # the loop is still running.
    table.update_item(
        Key=_query_key(user_id, conv_id, query_id),
        UpdateExpression="SET #s = :s, trace_s3_key = :tr, updated_at = :u",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": status, ":tr": trace_key, ":u": utc_now()},
    )


def _finalize_query(user_id, conv_id, query_id, answer_key, tokens_used, artifacts):
    table.update_item(
        Key=_query_key(user_id, conv_id, query_id),
        UpdateExpression=(
            "SET #s = :s, answer_s3_key = :a, tokens_used = :t, "
            "artifacts = :f, updated_at = :u"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": "complete",
            ":a": answer_key,
            ":t": tokens_used,
            ":f": artifacts,
            ":u": utc_now(),
        },
    )


def _fail_query(user_id, conv_id, query_id, reason):
    table.update_item(
        Key=_query_key(user_id, conv_id, query_id),
        UpdateExpression="SET #s = :s, status_message = :m, updated_at = :u",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": "failed",
            ":m": str(reason)[:500],
            ":u": utc_now(),
        },
    )


def accumulate_tokens(user_id, input_tokens, output_tokens):
    """Add the consumed tokens to the user's lifetime usage record."""
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


def _history(user_id, conv_id, current_query_id):
    """Return recent completed exchanges as Converse messages (text only)."""
    items = table.query(
        KeyConditionExpression=(
            Key("pk").eq(f"USER#{user_id}") &
            Key("sk").begins_with(f"QUERY#{conv_id}#")
        )
    ).get("Items", [])
    done = sorted(
        (i for i in items
         if i.get("status") == "complete" and i.get("query_id") != current_query_id
         and i.get("question_s3_key") and i.get("answer_s3_key")),
        key=lambda i: i.get("created_at") or "",
    )[-HISTORY_WINDOW:]

    messages = []
    for item in done:
        try:
            question = _read_s3_text(item["question_s3_key"])
            answer = _read_s3_text(item["answer_s3_key"])
        except Exception:
            continue
        messages.append({"role": "user", "content": [{"text": question}]})
        messages.append({"role": "assistant", "content": [{"text": answer}]})
    return messages


# ================================================================================
# File type detection
# ================================================================================

# Leading bytes of the types worth recognising. The sandbox guesses from the
# extension, and a cell may well write a PNG to `out` or `plot.dat`.
MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
)


def _sniff(body, declared):
    """Decide a file's type from its content, falling back to its name."""
    for prefix, mime in MAGIC:
        if body.startswith(prefix):
            return mime
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    # Textual types before any image/* guess: an SVG sent as an image block
    # would be rejected, and it reads fine as text.
    if b"\x00" not in body[:8192]:
        try:
            body[:8192].decode("utf-8")
            if declared.startswith("text/") or declared in (
                    "application/json", "application/xml", "image/svg+xml"):
                return declared
            return "text/plain"
        except UnicodeDecodeError:
            pass
    return declared


def _png_too_large(body):
    """True when a PNG exceeds the Converse per-side pixel limit."""
    if not body.startswith(b"\x89PNG") or len(body) < 24:
        return False
    width, height = struct.unpack(">II", body[16:24])
    return width > IMAGE_PX_LIMIT or height > IMAGE_PX_LIMIT


# ================================================================================
# The tool loop
# ================================================================================

class Run:
    """State for one query: the sandbox, the trace, artifacts and deadlines."""

    def __init__(self, user_id, conv_id, query_id, context):
        self.user_id = user_id
        self.conv_id = conv_id
        self.prefix = _s3_prefix(user_id, conv_id, query_id)
        self.trace_key = f"{self.prefix}/trace.json"
        self.context = context
        self.session = None
        self.steps = []
        self.artifacts = []
        self.usage = {"input": 0, "output": 0}

    # --------------------------------------------------------------------------
    # Time and progress
    # --------------------------------------------------------------------------

    def remaining_ms(self):
        return self.context.get_remaining_time_in_millis() - RESERVE_MS

    def deadline(self, seconds):
        """A time.monotonic() deadline: `seconds` away, or sooner if the Lambda is."""
        budget = min(seconds, max(self.remaining_ms() / 1000 - 30, 5))
        return time.monotonic() + budget

    def step(self, **step):
        """Record a trace step and persist the trace for the polling browser."""
        self.steps.append(step)
        try:
            _write_s3_json(self.trace_key, self.steps)
        except Exception:
            logger.exception("Progress write failed (non-fatal)")

    # --------------------------------------------------------------------------
    # Sandbox
    # --------------------------------------------------------------------------

    def sandbox(self):
        """Return the conversation's sandbox, launching it on first use.

        Returns:
            (session, note) where note is a message for the model when a fresh
            VM replaced an expired one, else "".
        """
        if self.session is not None:
            return self.session, ""
        session, launch = sandbox.ensure(self.user_id, self.conv_id)
        note = ""
        if launch:
            self.step(type="sandbox",
                      text=f"Launched MicroVM {launch['launched']} "
                           f"({launch['ms'] / 1000:.1f}s from snapshot)")
            if launch["replaced"]:
                note = ("[The previous sandbox had expired, so this is a fresh "
                        "one: variables and files from earlier messages are "
                        "gone.]\n")
        elif session.get("state") == "SUSPENDED":
            # The next request is what wakes it; say so in the trace, since
            # that is the MicroVM behaviour worth seeing.
            self.step(type="sandbox",
                      text=f"Resuming suspended MicroVM {session['id']} "
                           "with its Python state intact")
        else:
            self.step(type="sandbox", text=f"Reusing MicroVM {session['id']}")
        self.session = session
        return session, note

    # --------------------------------------------------------------------------
    # Tools
    # --------------------------------------------------------------------------

    def run_tool(self, name, args):
        """Execute one tool call. Returns (content_blocks, ok)."""
        if name == "run_code":
            return self.tool_run_code(str(args.get("code") or ""))
        if name == "get_result":
            return self.tool_get_result(str(args.get("job") or ""))
        if name == "show_file":
            return self.tool_show_file(str(args.get("path") or ""))
        return [{"text": f"Unknown tool {name}"}], False

    def _cell_outcome(self, status, job, note=""):
        """Turn a /result response into tool-result content."""
        state = status.get("state")
        if state == "running":
            text = (f"Still running after {status.get('elapsed_s', '?')}s (job {job}). "
                    "Call get_result with this job id to keep waiting.")
            self.step(type="tool_result", text=text, ok=True)
            return [{"text": note + text}], True
        if state != "done":
            text = (f"Job {job} is unknown to the sandbox -- it was replaced "
                    "since the job started. Run the code again.")
            self.step(type="tool_result", text=text, ok=False)
            return [{"text": note + text}], False
        result = status.get("result") or {}
        ok = bool(result.get("ok"))
        output = result.get("stdout") or "(no output)"
        ms = result.get("execution_ms")
        self.step(type="tool_result", text=output[:2000], ok=ok, ms=ms)
        return [{"text": note + _clip(output)}], ok

    def tool_run_code(self, code):
        if not code.strip():
            return [{"text": "No code given."}], False
        if len(code) > 50000:
            return [{"text": "Cell too large (50,000 character limit)."}], False
        session, note = self.sandbox()
        submitted = sandbox.submit(session, code)
        if submitted.get("state") == "refused":
            text = submitted.get("error", "The sandbox refused the cell.")
            self.step(type="tool_result", text=text, ok=False)
            return [{"text": note + text}], False
        job = submitted["job"]
        status = sandbox.wait(session, job, self.deadline(TOOL_WAIT_SECONDS))
        return self._cell_outcome(status, job, note)

    def tool_get_result(self, job):
        session, note = self.sandbox()
        status = sandbox.wait(session, job, self.deadline(TOOL_WAIT_SECONDS))
        return self._cell_outcome(status, job, note)

    def tool_show_file(self, path):
        if not path:
            return [{"text": "No path given."}], False
        session, note = self.sandbox()
        body, declared, name = sandbox.fetch_file(session, path)
        mime = _sniff(body, declared)

        # Re-showing a path replaces its attachment: after a fix-and-rerender
        # the user should see the corrected image, not both versions.
        artifact = next((a for a in self.artifacts if a["path"] == path), None)
        replaced = artifact is not None
        if not replaced:
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:80] or "file"
            artifact = {"key": f"{self.prefix}/files/{len(self.artifacts) + 1}-{safe}",
                        "path": path}
            self.artifacts.append(artifact)
        artifact.update(name=name, mime=mime, size=len(body))
        s3.put_object(Bucket=BACKEND_BUCKET, Key=artifact["key"], Body=body, ContentType=mime)
        self.step(type="file", name=name, mime=mime, size=len(body), replaced=replaced)

        fmt = IMAGE_FORMATS.get(mime)
        if fmt and len(body) <= IMAGE_BYTES_LIMIT and not _png_too_large(body):
            return [{"text": f"{note}Shown to the user: {name} ({len(body):,} bytes). "
                             "Here it is so you can check it."},
                    {"image": {"format": fmt, "source": {"bytes": body}}}], True
        if mime.startswith("text/") or mime in ("application/json", "image/svg+xml"):
            text = body.decode("utf-8", errors="replace")
            return [{"text": f"{note}Attached {name} for the user. Contents:\n"
                             + _clip(text)}], True
        return [{"text": f"{note}Attached {name} ({mime}, {len(body):,} bytes) "
                         "for the user to download."}], True

    # --------------------------------------------------------------------------
    # Converse loop
    # --------------------------------------------------------------------------

    def converse(self, messages):
        """Run the model/tool loop to a final answer. Returns the answer text."""
        for turn in range(MAX_TURNS):
            if self.remaining_ms() < 30_000:
                return self._stopped("ran out of time")

            response = bedrock.converse(
                modelId=MODEL_ID,
                system=[{"text": SYSTEM_PROMPT}],
                messages=messages,
                toolConfig={"tools": TOOLS},
                inferenceConfig={"maxTokens": 8192},
            )
            usage = response.get("usage") or {}
            self.usage["input"] += int(usage.get("inputTokens") or 0)
            self.usage["output"] += int(usage.get("outputTokens") or 0)

            message = response["output"]["message"]
            messages.append(message)
            blocks = message.get("content") or []
            text = "\n".join(b["text"] for b in blocks if "text" in b).strip()
            calls = [b["toolUse"] for b in blocks if "toolUse" in b]

            if not calls:
                if response.get("stopReason") == "max_tokens":
                    text += "\n\n_(The answer hit the output limit and was cut short.)_"
                return text or "(the model returned no answer)"

            if text:
                self.step(type="reasoning", text=text)

            results = []
            for call in calls:
                args = call.get("input") or {}
                self.step(type="tool_call", tool=call["name"], input=args)
                try:
                    content, ok = self.run_tool(call["name"], args)
                except Exception as exc:
                    # A tool failure is information for the model, not the end
                    # of the query: it can retry, or explain what went wrong.
                    logger.exception("Tool %s failed", call["name"])
                    content, ok = [{"text": f"Tool error: {exc}"}], False
                    self.step(type="tool_result", text=f"Tool error: {exc}", ok=False)
                results.append({"toolResult": {
                    "toolUseId": call["toolUseId"], "content": content,
                    "status": "success" if ok else "error"}})
            messages.append({"role": "user", "content": results})

        return self._stopped(f"reached the {MAX_TURNS}-step limit")

    def _stopped(self, why):
        last = next((s["text"] for s in reversed(self.steps)
                     if s.get("type") == "reasoning"), "")
        return (f"I stopped because I {why}. " + last).strip()


# ================================================================================
# Core: one query
# ================================================================================

def process_query(user_id, conv_id, query_id, context):
    """Run the tool loop for one query and persist answer, trace and files."""
    run = Run(user_id, conv_id, query_id, context)
    _update_query_status(user_id, conv_id, query_id, "processing", run.trace_key)

    try:
        question = _read_s3_text(f"{run.prefix}/question.txt").strip()
    except Exception as exc:
        logger.exception("Failed to read question from S3")
        _fail_query(user_id, conv_id, query_id, f"Could not read question: {exc}")
        return

    if not question:
        _fail_query(user_id, conv_id, query_id, "Question is empty")
        return

    t0 = time.time()
    messages = _history(user_id, conv_id, query_id)
    messages.append({"role": "user", "content": [{"text": question}]})

    try:
        answer = run.converse(messages)
    except Exception as exc:
        logger.exception("Converse loop failed")
        accumulate_tokens(user_id, run.usage["input"], run.usage["output"])
        _fail_query(user_id, conv_id, query_id, f"Model call failed: {exc}")
        return

    run.steps.append({"type": "answer"})   # marks the end of the trace for the UI
    logger.info(
        "Query complete. conv=%s query=%s steps=%d files=%d elapsed=%.1fs in=%d out=%d",
        conv_id, query_id, len(run.steps), len(run.artifacts), time.time() - t0,
        run.usage["input"], run.usage["output"],
    )

    answer_key = f"{run.prefix}/answer.txt"
    try:
        _write_s3_text(answer_key, answer)
        _write_s3_json(run.trace_key, run.steps)
    except Exception as exc:
        logger.exception("Failed to write answer/trace to S3")
        _fail_query(user_id, conv_id, query_id, f"Failed to store result: {exc}")
        return

    total_tokens = run.usage["input"] + run.usage["output"]
    _finalize_query(user_id, conv_id, query_id, answer_key, total_tokens, run.artifacts)
    accumulate_tokens(user_id, run.usage["input"], run.usage["output"])


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
            process_query(user_id, conv_id, query_id, context)
        except Exception:
            logger.exception("Unhandled error processing SQS record")

    return {"statusCode": 200}
