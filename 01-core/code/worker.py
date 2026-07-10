# ================================================================================
# worker.py
#
# Purpose
# SQS-triggered RAG worker. For each query message:
#   1. Load corpus embeddings from S3 into memory
#   2. Embed the user question via Bedrock Titan
#   3. Cosine similarity search → top-k chunks
#   4. Fetch last N completed Q&A pairs from this conversation as history
#   5. Build stateful prompt: system + history + retrieved context + question
#   6. Call Bedrock Haiku for the answer
#   7. Write answer.txt and sources.json to S3
#   8. Update DynamoDB query record and accumulate tokens on user usage
#
# Expected SQS message body
#   {"user_id": "...", "conv_id": "...", "query_id": "..."}
# ================================================================================

import io
import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3
import numpy as np
from boto3.dynamodb.conditions import Key
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

bedrock = boto3.client(
    "bedrock-runtime",
    config=Config(read_timeout=240, connect_timeout=10),
)
s3 = boto3.client("s3")

# ================================================================================
# Environment
# ================================================================================

BACKEND_BUCKET   = os.environ["BACKEND_BUCKET_NAME"]
CHAT_MODEL_ID    = os.environ["BEDROCK_MODEL_ID"]
EMBED_MODEL_ID   = "amazon.titan-embed-text-v2:0"

# ================================================================================
# Constants
# ================================================================================

TOP_K          = 20    # chunks retrieved per query
HISTORY_WINDOW = 5     # prior Q&A pairs injected as conversation history
MAX_CHUNK_CHARS = 1500 # truncate individual chunks before injection


# ================================================================================
# Generic helpers
# ================================================================================

def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _read_s3_bytes(key):
    result = s3.get_object(Bucket=BACKEND_BUCKET, Key=key)
    return result["Body"].read()


def _read_s3_text(key):
    return _read_s3_bytes(key).decode("utf-8")


def _write_s3_text(key, text):
    s3.put_object(
        Bucket=BACKEND_BUCKET,
        Key=key,
        Body=text.encode("utf-8"),
        ContentType="text/plain; charset=utf-8",
    )


def _write_s3_json(key, obj):
    s3.put_object(
        Bucket=BACKEND_BUCKET,
        Key=key,
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
        Key={
            "pk": f"USER#{user_id}",
            "sk": f"QUERY#{conv_id}#{query_id}",
        },
        UpdateExpression="SET #s = :s, updated_at = :u",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": status, ":u": utc_now()},
    )


def _finalize_query(user_id, conv_id, query_id,
                    answer_key, sources_key, tokens_used):
    table.update_item(
        Key={
            "pk": f"USER#{user_id}",
            "sk": f"QUERY#{conv_id}#{query_id}",
        },
        UpdateExpression=(
            "SET #s = :s, answer_s3_key = :a, "
            "sources_s3_key = :src, tokens_used = :t, updated_at = :u"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s":   "complete",
            ":a":   answer_key,
            ":src": sources_key,
            ":t":   tokens_used,
            ":u":   utc_now(),
        },
    )


def _fail_query(user_id, conv_id, query_id, reason):
    table.update_item(
        Key={
            "pk": f"USER#{user_id}",
            "sk": f"QUERY#{conv_id}#{query_id}",
        },
        UpdateExpression="SET #s = :s, status_message = :m, updated_at = :u",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": "failed",
            ":m": str(reason)[:500],
            ":u": utc_now(),
        },
    )


def accumulate_tokens(user_id, input_tokens, output_tokens):
    """Add consumed tokens to the user's lifetime usage record."""
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
        # Best-effort — never let token tracking block query completion
        logger.exception(
            "Failed to update token usage for user_id=%s", user_id
        )


# ================================================================================
# Corpus loading
# ================================================================================

def _load_corpus():
    """
    Load chunk metadata and embeddings from S3 into memory.
    Both objects are written by the ingest script before first use.
    """
    chunks_bytes     = _read_s3_bytes("corpus/chunks.json")
    embeddings_bytes = _read_s3_bytes("corpus/embeddings.npy")

    chunks     = json.loads(chunks_bytes.decode("utf-8"))
    embeddings = np.load(io.BytesIO(embeddings_bytes)).astype(np.float32)

    return chunks, embeddings


# ================================================================================
# Embedding + retrieval
# ================================================================================

def _embed_query(question):
    """Embed a question string via Bedrock Titan Embeddings v2."""
    body = json.dumps({
        "inputText": question,
        "dimensions": 1024,
        "normalize": True,
    })

    response = bedrock.invoke_model(
        modelId=EMBED_MODEL_ID,
        body=body,
        contentType="application/json",
        accept="application/json",
    )

    payload = json.loads(response["body"].read())
    return np.array(payload["embedding"], dtype=np.float32)


def _cosine_search(query_vec, embeddings, chunks, top_k):
    """Return indices of top_k most similar rows by cosine similarity."""
    scores  = embeddings @ query_vec
    indices = np.argsort(scores)[::-1][:top_k]
    return indices.tolist(), scores[indices].tolist()


MIN_CHUNKS_PER_NAMED_REPO = 2  # guaranteed slots when a repo is named


def _repos_mentioned_in_question(question, chunks):
    """
    Return the set of repo names that appear verbatim in the question.
    Only considers repos that actually exist in the corpus.
    """
    known_repos = {c.get("repo", "") for c in chunks}
    q_lower     = question.lower()
    return {r for r in known_repos if r and r in q_lower}


def _retrieve_chunks(question, chunks, embeddings):
    """
    Embed question and return top-k chunk dicts with scores.

    When the question explicitly names specific repos, ensure each
    gets at least MIN_CHUNKS_PER_NAMED_REPO slots so a multi-repo
    comparison query cannot crowd out any one provider.
    """
    query_vec = _embed_query(question)
    indices, scores = _cosine_search(query_vec, embeddings, chunks, TOP_K)

    selected_indices = set(indices)
    results = []
    for idx, score in zip(indices, scores):
        chunk = dict(chunks[idx])
        chunk["score"] = round(float(score), 4)
        results.append(chunk)

    # Guarantee coverage for explicitly named repos
    mentioned = _repos_mentioned_in_question(question, chunks)
    for repo in mentioned:
        present = sum(1 for c in results if c.get("repo") == repo)
        if present >= MIN_CHUNKS_PER_NAMED_REPO:
            continue
        # Find the best-scoring chunks for this repo not already selected
        repo_hits = [
            (i, float(scores[list(indices).index(i)]) if i in indices else float(embeddings[i] @ query_vec))
            for i, c in enumerate(chunks)
            if c.get("repo") == repo and i not in selected_indices
        ]
        repo_hits.sort(key=lambda x: x[1], reverse=True)
        needed = MIN_CHUNKS_PER_NAMED_REPO - present
        for idx2, score2 in repo_hits[:needed]:
            chunk = dict(chunks[idx2])
            chunk["score"] = round(score2, 4)
            results.append(chunk)
            selected_indices.add(idx2)

    return results


# ================================================================================
# Inventory queries — bypass cosine search, enumerate corpus directly
# ================================================================================

_GITHUB_TRIGGERS  = {"github", "repo", "repos", "repository", "repositories",
                     "project", "projects", "portfolio"}
_YOUTUBE_TRIGGERS = {"youtube", "video", "videos", "channel"}
_LIST_TRIGGERS    = {"complete", "full", "all", "every", "list", "inventory"}


def _is_github_inventory_query(question):
    words = set(question.lower().split())
    return bool(words & _GITHUB_TRIGGERS) and bool(words & _LIST_TRIGGERS)


def _is_youtube_inventory_query(question):
    words = set(question.lower().split())
    return bool(words & _YOUTUBE_TRIGGERS) and bool(words & _LIST_TRIGGERS)


_CLOUD_PREFIXES = ["aws-", "gcp-", "azure-", "oci-"]
_CLOUD_ORDER    = ["aws", "gcp", "azure", "oci"]

# Repos with different per-cloud suffixes that represent the same architecture
_ARCH_ALIASES = {
    "rstudio-eks":      "rstudio-k8s",
    "rstudio-gke":      "rstudio-k8s",
    "rstudio-aks":      "rstudio-k8s",
    "mig":              "autoscaling",
    "vmss":             "autoscaling",
    "instance-pool":    "autoscaling",
    "flask-mig":        "flask-asg",
    "flask-vmss":       "flask-asg",
    "directory":        "active-directory",
    "managed-ad":       "active-directory",
    "virtual-desktops": "workspaces",
    "identity-app":     "cognito-app",
    "entra-app":        "cognito-app",
    "mesh":             "transit-gateway",
    "wlan":             "transit-gateway",
    "filestore":        "efs",
    "nfs-files":        "efs",
    "fss":              "efs",
    "dms":              "data-sync",
    "pubsub-keygen":    "sqs-keygen",
    "sb-keygen":        "sqs-keygen",
}

_ARCH_DISPLAY = {
    "postgres":         "PostgreSQL",
    "mysql":            "MySQL",
    "sqlserver":        "SQL Server",
    "rstudio-cluster":  "RStudio cluster",
    "rstudio-k8s":      "RStudio on Kubernetes",
    "k8s":              "Kubernetes",
    "autoscaling":      "Autoscaling (ASG / MIG / VMSS / Pools)",
    "flask-asg":        "Autoscaled Flask app",
    "flask-container":  "Containerized Flask app",
    "packer":           "Image pipelines (Packer)",
    "active-directory": "Managed directory services",
    "mini-ad":          "Minimal AD (lab-scale)",
    "workspaces":       "Managed virtual desktops",
    "xubuntu-xrdp":     "Linux remote desktops (xRDP)",
    "cognito-app":      "App auth (Cognito / Identity Platform / Entra)",
    "transit-gateway":  "Hub-and-spoke networking",
    "efs":              "Shared file systems (EFS / Filestore / Files / FSS)",
    "data-sync":        "Data migration",
    "crud-example":     "Serverless CRUD API",
    "serverless-mcp":   "Serverless MCP service",
    "sqs-keygen":       "Queue-driven workers (SQS / Pub/Sub / Service Bus)",
    "cartoonify":       "Cartoonify (vision AI image transformation)",
    "openclaw":         "OpenClaw agent deployment",
    "resume-app":       "Resume app (LLM document processing)",
    "rag-demo":         "RAG demo (Meet Mike)",
    "ask-mike":         "Ask Mike (this app)",
}

_ARCH_CATEGORY = {
    "postgres":         "Databases & Analytics",
    "mysql":            "Databases & Analytics",
    "sqlserver":        "Databases & Analytics",
    "rstudio-cluster":  "Databases & Analytics",
    "rstudio-k8s":      "Databases & Analytics",
    "k8s":              "Compute & Containers",
    "autoscaling":      "Compute & Containers",
    "flask-asg":        "Compute & Containers",
    "flask-container":  "Compute & Containers",
    "packer":           "Compute & Containers",
    "active-directory": "Identity & Desktops",
    "mini-ad":          "Identity & Desktops",
    "workspaces":       "Identity & Desktops",
    "xubuntu-xrdp":     "Identity & Desktops",
    "cognito-app":      "Identity & Desktops",
    "transit-gateway":  "Networking & Storage",
    "efs":              "Networking & Storage",
    "data-sync":        "Networking & Storage",
    "crud-example":     "Serverless",
    "serverless-mcp":   "Serverless",
    "sqs-keygen":       "Serverless",
    "cartoonify":       "AI Services",
    "openclaw":         "AI Services",
    "resume-app":       "AI Services",
    "rag-demo":         "AI Services",
    "ask-mike":         "AI Services",
}

_CATEGORY_ORDER = [
    "Databases & Analytics",
    "Compute & Containers",
    "Identity & Desktops",
    "Networking & Storage",
    "Serverless",
    "AI Services",
    "Other",
]


def _build_github_inventory(chunks):
    """Return a cross-cloud matrix of every GitHub repo in the corpus."""
    # Collect all GitHub repos: repo_name → root URL
    repos = {}
    for chunk in chunks:
        repo = chunk.get("repo", "")
        if repo in ("resume", "youtube", "") or repo in repos:
            continue
        url  = chunk.get("source_url", "")
        repos[repo] = url.split("/blob/")[0] if "/blob/" in url else url

    # Parse each repo into cloud + base architecture
    # matrix[base_arch][cloud] = url
    matrix       = {}
    uncategorized = {}

    for repo_name, url in repos.items():
        cloud = base = None
        for prefix in _CLOUD_PREFIXES:
            if repo_name.startswith(prefix):
                cloud = prefix.rstrip("-")
                base  = _ARCH_ALIASES.get(repo_name[len(prefix):],
                                          repo_name[len(prefix):])
                break
        if cloud is None:
            uncategorized[repo_name] = url
            continue
        if base not in matrix:
            matrix[base] = {}
        matrix[base][cloud] = url

    # Group architectures by category
    by_category = {cat: [] for cat in _CATEGORY_ORDER}
    for base, cloud_map in matrix.items():
        cat = _ARCH_CATEGORY.get(base, "Other")
        by_category[cat].append((base, cloud_map))

    total_repos = len(repos)
    lines = [f"## GitHub Portfolio — {total_repos} repositories\n",
             "**Legend:** ✅ built & deployable · 🚧 planned / not yet built\n"]

    for cat in _CATEGORY_ORDER:
        archs = sorted(by_category[cat],
                       key=lambda x: _ARCH_DISPLAY.get(x[0], x[0]))
        if not archs:
            continue
        lines.append(f"\n### {cat}\n")
        lines.append("| Architecture | AWS | GCP | Azure | OCI |")
        lines.append("|---|:---:|:---:|:---:|:---:|")
        for base, cloud_map in archs:
            display = _ARCH_DISPLAY.get(base, base.replace("-", " ").title())
            cells   = []
            for cloud in _CLOUD_ORDER:
                u = cloud_map.get(cloud)
                cells.append(f"[✅]({u})" if u else "🚧")
            lines.append(f"| {display} | {' | '.join(cells)} |")

    # Surface repos that don't follow the <cloud>-<arch> naming pattern
    if uncategorized:
        lines.append("\n### Other\n")
        for name in sorted(uncategorized):
            u = uncategorized[name]
            lines.append(f"- [{name}]({u})" if u else f"- {name}")

    return "\n".join(lines)


def _build_youtube_inventory(chunks):
    """Return a markdown list of every unique YouTube video in the corpus."""
    seen = {}
    for chunk in chunks:
        if chunk.get("repo") != "youtube":
            continue
        title = chunk.get("title", "").strip()
        url   = chunk.get("source_url", "").strip()
        if title and url and title not in seen:
            seen[title] = url

    lines = [f"Here are all {len(seen)} YouTube videos on my channel:\n"]
    for title in sorted(seen.keys()):
        lines.append(f"- [{title}]({seen[title]})")
    return "\n".join(lines)


# ================================================================================
# Conversation history
# ================================================================================

def _fetch_history(user_id, conv_id, exclude_query_id):
    """
    Return the last HISTORY_WINDOW completed Q&A pairs for this
    conversation, oldest first, excluding the current query.
    """
    result = table.query(
        KeyConditionExpression=(
            Key("pk").eq(f"USER#{user_id}") &
            Key("sk").begins_with(f"QUERY#{conv_id}#")
        )
    )

    items = [
        item for item in result.get("Items", [])
        if item.get("status") == "complete"
        and item.get("query_id") != exclude_query_id
    ]

    # Oldest first, then take the last HISTORY_WINDOW
    items.sort(key=lambda x: x.get("created_at") or "")
    items = items[-HISTORY_WINDOW:]

    history = []
    for item in items:
        question = answer = None

        if item.get("question_s3_key"):
            try:
                question = _read_s3_text(item["question_s3_key"])
            except Exception:
                pass

        if item.get("answer_s3_key"):
            try:
                answer = _read_s3_text(item["answer_s3_key"])
            except Exception:
                pass

        if question and answer:
            history.append({"question": question, "answer": answer})

    return history


# ================================================================================
# Bedrock Haiku call
# ================================================================================

SYSTEM_PROMPT = """You are an AI assistant for Mike Monaco's cloud \
architecture portfolio and YouTube channel. Mike is a principal-level cloud \
architect who has published 100+ open-source reference architectures across \
AWS, GCP, Azure, and OCI, and produces technical YouTube walkthroughs at \
Mike's Cloud Solutions.

Your job is to answer questions about Mike's published projects, reference \
architectures, YouTube videos, and technical background — grounded strictly \
in the provided context excerpts from his GitHub repos and YouTube content.

If a specific project, repository, or topic appears in the retrieved \
context, describe it accurately and in detail. If the context does not \
mention a specific project or topic, say so directly — do not speculate \
about what it might contain or describe what it would look like. A clear \
"I don't have that in my corpus right now" is always better than a \
plausible-sounding guess."""


def _call_haiku(question, retrieved_chunks, history):
    """
    Build the messages array with history + context and call Bedrock Haiku.
    Returns (answer_text, input_tokens, output_tokens).
    """
    # Build context block from retrieved chunks
    context_parts = []
    for i, chunk in enumerate(retrieved_chunks, 1):
        source = chunk.get("source_url") or chunk.get("repo") or "unknown"
        text   = (chunk.get("text") or "")[:MAX_CHUNK_CHARS]
        context_parts.append(f"[{i}] Source: {source}\n{text}")

    context_block = "\n\n---\n\n".join(context_parts)

    # Inject prior turns as alternating user/assistant messages
    messages = []
    for turn in history:
        messages.append({"role": "user",      "content": turn["question"]})
        messages.append({"role": "assistant", "content": turn["answer"]})

    # Current question with retrieved context appended
    user_content = f"""Context excerpts from Mike's portfolio:

{context_block}

---

Question: {question}"""

    messages.append({"role": "user", "content": user_content})

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1024,
        "temperature": 0.3,
        "system": SYSTEM_PROMPT,
        "messages": messages,
    }

    logger.info(
        "Haiku call starting. turns=%d chunks=%d",
        len(history),
        len(retrieved_chunks),
    )

    t0 = time.time()

    response = bedrock.invoke_model(
        modelId=CHAT_MODEL_ID,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )

    elapsed = time.time() - t0
    payload = json.loads(response["body"].read())
    usage   = payload.get("usage", {})

    logger.info(
        "Haiku call complete. elapsed=%.1fs input=%s output=%s",
        elapsed,
        usage.get("input_tokens"),
        usage.get("output_tokens"),
    )

    answer = payload["content"][0]["text"].strip()
    return answer, usage.get("input_tokens", 0), usage.get("output_tokens", 0)


# ================================================================================
# Core pipeline
# ================================================================================

def process_query(user_id, conv_id, query_id):
    """Run the full RAG pipeline for one query."""

    _update_query_status(user_id, conv_id, query_id, "processing")

    prefix = _s3_prefix(user_id, conv_id, query_id)

    # -------------------------------------------------------------------------
    # Read question from S3
    # -------------------------------------------------------------------------

    try:
        question = _read_s3_text(f"{prefix}/question.txt").strip()
    except Exception as exc:
        logger.exception("Failed to read question from S3")
        _fail_query(user_id, conv_id, query_id, f"Could not read question: {exc}")
        return

    if not question:
        _fail_query(user_id, conv_id, query_id, "Question is empty")
        return

    # -------------------------------------------------------------------------
    # Load corpus
    # -------------------------------------------------------------------------

    try:
        chunks, embeddings = _load_corpus()
        logger.info("Corpus loaded. chunks=%d", len(chunks))
    except Exception as exc:
        logger.exception("Failed to load corpus from S3")
        _fail_query(user_id, conv_id, query_id, f"Corpus unavailable: {exc}")
        return

    # -------------------------------------------------------------------------
    # Inventory short-circuit — skip embedding + Haiku for list-all queries
    # -------------------------------------------------------------------------

    inventory_answer = None
    if _is_youtube_inventory_query(question):
        inventory_answer = _build_youtube_inventory(chunks)
    elif _is_github_inventory_query(question):
        inventory_answer = _build_github_inventory(chunks)

    if inventory_answer is not None:
        prefix      = _s3_prefix(user_id, conv_id, query_id)
        answer_key  = f"{prefix}/answer.txt"
        sources_key = f"{prefix}/sources.json"
        try:
            _write_s3_text(answer_key, inventory_answer)
            _write_s3_json(sources_key, [])
        except Exception as exc:
            logger.exception("Failed to write inventory answer to S3")
            _fail_query(user_id, conv_id, query_id, f"Failed to store result: {exc}")
            return
        _finalize_query(user_id, conv_id, query_id, answer_key, sources_key, 0)
        logger.info("Inventory query complete. user=%s conv=%s query=%s",
                    user_id, conv_id, query_id)
        return

    # -------------------------------------------------------------------------
    # Retrieve relevant chunks
    # -------------------------------------------------------------------------

    try:
        retrieved = _retrieve_chunks(question, chunks, embeddings)
        logger.info("Retrieved %d chunks", len(retrieved))
    except Exception as exc:
        logger.exception("Retrieval failed")
        _fail_query(user_id, conv_id, query_id, f"Retrieval failed: {exc}")
        return

    # -------------------------------------------------------------------------
    # Fetch conversation history
    # -------------------------------------------------------------------------

    try:
        history = _fetch_history(user_id, conv_id, exclude_query_id=query_id)
        logger.info("History fetched. turns=%d", len(history))
    except Exception as exc:
        # Non-fatal — proceed without history rather than failing the query
        logger.exception("Failed to fetch history; continuing without it")
        history = []

    # -------------------------------------------------------------------------
    # Call Haiku
    # -------------------------------------------------------------------------

    try:
        answer, input_tokens, output_tokens = _call_haiku(
            question, retrieved, history
        )
    except Exception as exc:
        logger.exception("Haiku call failed")
        _fail_query(user_id, conv_id, query_id, f"Model call failed: {exc}")
        return

    # -------------------------------------------------------------------------
    # Persist answer and sources to S3
    # -------------------------------------------------------------------------

    answer_key  = f"{prefix}/answer.txt"
    sources_key = f"{prefix}/sources.json"

    sources_payload = [
        {
            "repo":       c.get("repo"),
            "file":       c.get("file"),
            "title":      c.get("title"),
            "source_url": c.get("source_url"),
            "score":      c.get("score"),
        }
        for c in retrieved
    ]

    try:
        _write_s3_text(answer_key, answer)
        _write_s3_json(sources_key, sources_payload)
    except Exception as exc:
        logger.exception("Failed to write answer/sources to S3")
        _fail_query(user_id, conv_id, query_id, f"Failed to store result: {exc}")
        return

    # -------------------------------------------------------------------------
    # Finalise DynamoDB record and accumulate tokens
    # -------------------------------------------------------------------------

    total_tokens = int(input_tokens or 0) + int(output_tokens or 0)

    _finalize_query(
        user_id, conv_id, query_id,
        answer_key, sources_key, total_tokens,
    )

    accumulate_tokens(user_id, input_tokens, output_tokens)

    logger.info(
        "Query complete. user=%s conv=%s query=%s tokens=%d",
        user_id, conv_id, query_id, total_tokens,
    )


# ================================================================================
# Lambda entry point
# ================================================================================

def lambda_handler(event, context):
    """
    SQS-triggered entry point. Each record is processed independently so
    one bad message does not block the rest of the batch.
    """
    for record in event.get("Records", []):
        try:
            message  = json.loads(record["body"])
            user_id  = str(message.get("user_id",  "")).strip()
            conv_id  = str(message.get("conv_id",  "")).strip()
            query_id = str(message.get("query_id", "")).strip()

            if not user_id or not conv_id or not query_id:
                logger.error("Message missing required fields: %s", message)
                continue

            logger.info(
                "Processing query. user=%s conv=%s query=%s",
                user_id, conv_id, query_id,
            )
            process_query(user_id, conv_id, query_id)

        except Exception:
            logger.exception("Unhandled error processing SQS record")

    return {"statusCode": 200}
