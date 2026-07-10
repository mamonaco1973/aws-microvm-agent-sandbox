#!/usr/bin/env python3
# ================================================================================
# ingest.py
#
# Purpose
# Builds the RAG corpus from Mike Monaco's public GitHub repos and YouTube
# channel, then uploads corpus/chunks.json and corpus/embeddings.npy to the
# backend S3 bucket.
#
# Run locally (not deployed to Lambda):
#   pip install -r requirements.txt
#   python ingest.py --bucket <backend-bucket-name> [--region us-east-1]
#
# What it ingests:
#   - README.md from every public mamonaco1973/* GitHub repo
#   - main.tf, variables.tf, outputs.tf from each repo's Terraform dirs
#   - YouTube video descriptions from the channel via YouTube Data API v3
#
# Prerequisites:
#   AWS credentials with s3:PutObject on the backend bucket
#   YOUTUBE_API_KEY env var (optional — skips YouTube if not set)
#   GITHUB_TOKEN env var (optional — raises API rate limit from 60 to 5000/hr)
# ================================================================================

import argparse
import io
import json
import logging
import os
import textwrap
import time
from pathlib import Path

import boto3
import numpy as np
import requests

# ================================================================================
# Logging
# ================================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ================================================================================
# Constants
# ================================================================================

GITHUB_ORG      = "mamonaco1973"
YOUTUBE_CHANNEL = "UCWt1avrTjFVJKfCC5CHxOFg"
EMBED_MODEL_ID  = "amazon.titan-embed-text-v2:0"
CHUNK_SIZE      = 800    # target words per chunk
CHUNK_OVERLAP   = 80     # words of overlap between consecutive chunks

# Documentation files indexed per repo
DOC_FILES = ["README.md", "CLAUDE.md"]

# ================================================================================
# GitHub helpers
# ================================================================================

def _gh_headers():
    token = os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def list_repos():
    """Return all public repos for the org."""
    repos = []
    page  = 1
    while True:
        url  = f"https://api.github.com/users/{GITHUB_ORG}/repos"
        resp = requests.get(url, headers=_gh_headers(),
                            params={"per_page": 100, "page": page, "type": "public"})
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        repos.extend(batch)
        page += 1
    return repos


def fetch_file(repo_name, path):
    """Return text content of a file, or None if it doesn't exist.

    Uses raw.githubusercontent.com to avoid the 60 req/hr API rate limit
    that applies to unauthenticated Contents API calls.
    """
    url  = f"https://raw.githubusercontent.com/{GITHUB_ORG}/{repo_name}/main/{path}"
    resp = requests.get(url)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.text


def fetch_doc_files(repo_name):
    """Return list of (path, content) for README.md and CLAUDE.md."""
    results = []
    for path in DOC_FILES:
        content = fetch_file(repo_name, path)
        if content:
            results.append((path, content))
    return results


# ================================================================================
# YouTube helpers
# ================================================================================

def fetch_youtube_descriptions(api_key):
    """Return list of {title, description, url} for all channel videos."""
    videos  = []
    page_token = None

    while True:
        params = {
            "key":        api_key,
            "channelId":  YOUTUBE_CHANNEL,
            "part":       "snippet",
            "type":       "video",
            "maxResults": 50,
        }
        if page_token:
            params["pageToken"] = page_token

        resp = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()

        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            video_id = item.get("id", {}).get("videoId")
            if not video_id:
                continue
            videos.append({
                "title":       snippet.get("title", ""),
                "description": snippet.get("description", ""),
                "url":         f"https://www.youtube.com/watch?v={video_id}",
            })

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return videos


# ================================================================================
# Chunking
# ================================================================================

def chunk_text(text, source_url, repo, file_path, title=""):
    """
    Split text into overlapping word-window chunks and return a list of
    chunk dicts ready for embedding.
    """
    words  = text.split()
    chunks = []
    start  = 0

    while start < len(words):
        end        = min(start + CHUNK_SIZE, len(words))
        chunk_text = " ".join(words[start:end])
        chunks.append({
            "text":       chunk_text,
            "source_url": source_url,
            "repo":       repo,
            "file":       file_path,
            "title":      title,
        })
        if end == len(words):
            break
        start = end - CHUNK_OVERLAP

    return chunks


# ================================================================================
# Bedrock embedding
# ================================================================================

def embed_chunks(chunks, region):
    """Return a float32 numpy array of shape (n_chunks, 1536)."""
    bedrock = boto3.client("bedrock-runtime", region_name=region)
    vectors = []

    for i, chunk in enumerate(chunks):
        if i % 25 == 0:
            log.info("Embedding chunk %d / %d …", i + 1, len(chunks))

        body = json.dumps({
            "inputText":  chunk["text"][:8000],   # Titan v2 max input
            "dimensions": 1024,
            "normalize":  True,
        })

        resp    = bedrock.invoke_model(
            modelId=EMBED_MODEL_ID,
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        payload = json.loads(resp["body"].read())
        vectors.append(payload["embedding"])

        # Stay within Titan's 2000 tokens/min rate limit at small scale
        time.sleep(0.05)

    return np.array(vectors, dtype=np.float32)


# ================================================================================
# S3 upload
# ================================================================================

def upload_corpus(bucket, chunks, embeddings, region):
    """Write chunks.json and embeddings.npy to corpus/ in the backend bucket."""
    s3 = boto3.client("s3", region_name=region)

    # chunks.json
    chunks_bytes = json.dumps(chunks, ensure_ascii=False).encode("utf-8")
    s3.put_object(
        Bucket=bucket,
        Key="corpus/chunks.json",
        Body=chunks_bytes,
        ContentType="application/json; charset=utf-8",
    )
    log.info("Uploaded corpus/chunks.json (%d bytes)", len(chunks_bytes))

    # embeddings.npy
    buf = io.BytesIO()
    np.save(buf, embeddings)
    buf.seek(0)
    s3.put_object(
        Bucket=bucket,
        Key="corpus/embeddings.npy",
        Body=buf.read(),
        ContentType="application/octet-stream",
    )
    log.info(
        "Uploaded corpus/embeddings.npy (shape=%s, dtype=%s)",
        embeddings.shape,
        embeddings.dtype,
    )


# ================================================================================
# Main pipeline
# ================================================================================

def build_corpus(bucket, region):
    chunks = []

    # -------------------------------------------------------------------------
    # GitHub repos
    # -------------------------------------------------------------------------

    log.info("Fetching public repos for %s …", GITHUB_ORG)
    repos = list_repos()
    log.info("Found %d repos", len(repos))

    for repo in repos:
        name     = repo["name"]
        html_url = repo["html_url"]

        # README.md and CLAUDE.md
        before = len(chunks)
        for doc_path, doc_content in fetch_doc_files(name):
            chunks.extend(chunk_text(
                doc_content,
                source_url=f"{html_url}/blob/main/{doc_path}",
                repo=name,
                file_path=doc_path,
                title=repo.get("description") or name,
            ))
        added = len(chunks) - before
        if added:
            log.info("  %s → %d chunks", name, added)

        # Avoid hammering the GitHub API
        time.sleep(0.3)

    log.info("GitHub: %d total chunks", len(chunks))

    # -------------------------------------------------------------------------
    # Local documents (resume, bio, etc.)
    # -------------------------------------------------------------------------

    local_docs = Path(__file__).parent.glob("*.txt")
    for doc_path in sorted(local_docs):
        content = doc_path.read_text(encoding="utf-8")
        before = len(chunks)
        chunks.extend(chunk_text(
            content,
            source_url="https://www.linkedin.com/in/michael-monaco/",
            repo="resume",
            file_path=doc_path.name,
            title=doc_path.stem,
        ))
        log.info("Local doc %s → %d chunks", doc_path.name, len(chunks) - before)

    # -------------------------------------------------------------------------
    # YouTube
    # -------------------------------------------------------------------------

    yt_key = os.environ.get("YOUTUBE_API_KEY")
    if yt_key:
        log.info("Fetching YouTube video descriptions …")
        try:
            videos = fetch_youtube_descriptions(yt_key)
            log.info("Found %d videos", len(videos))

            for v in videos:
                if not v["description"].strip():
                    continue
                text = f"{v['title']}\n\n{v['description']}"
                chunks.extend(chunk_text(
                    text,
                    source_url=v["url"],
                    repo="youtube",
                    file_path="video_description",
                    title=v["title"],
                ))
        except Exception as exc:
            log.warning("YouTube fetch failed (skipping): %s", exc)
    else:
        log.info("YOUTUBE_API_KEY not set — skipping YouTube ingestion")

    log.info("Total chunks to embed: %d", len(chunks))

    # -------------------------------------------------------------------------
    # Embed
    # -------------------------------------------------------------------------

    log.info("Embedding chunks via Bedrock Titan …")
    embeddings = embed_chunks(chunks, region)
    log.info("Embedding complete. shape=%s", embeddings.shape)

    # -------------------------------------------------------------------------
    # Upload
    # -------------------------------------------------------------------------

    log.info("Uploading corpus to s3://%s/corpus/ …", bucket)
    upload_corpus(bucket, chunks, embeddings, region)

    log.info("Ingestion complete. %d chunks indexed.", len(chunks))


# ================================================================================
# Entry point
# ================================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Build and upload RAG corpus to S3."
    )
    parser.add_argument(
        "--bucket",
        required=True,
        help="Backend S3 bucket name (e.g. rag-data-a1b2c3d4)",
    )
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="AWS region (default: us-east-1)",
    )
    args = parser.parse_args()

    build_corpus(args.bucket, args.region)
