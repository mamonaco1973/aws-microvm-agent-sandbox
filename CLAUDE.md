# CLAUDE.md

Guidance for working in **aws-microvm-agent-sandbox** (product name: **MicroVM Agent Sandbox**).
Read this before changing `01-sandbox/image/` or `02-core/code/worker.py`;
several things here look like obvious improvements and are not.

## What This App Is

A chat app whose agent runs code in its own **Lambda MicroVM**, one per
conversation, launched on demand. The demo prompt is *"Build me a fractal tree
and get me the results"*: the agent writes Python, runs it in the VM, looks at
the PNG it rendered, and attaches it to the answer.

The MicroVM plumbing (image build, supervisor, lifecycle, IAM) comes from the
sibling project `../aws-lambda-microvms`, whose CLAUDE.md records the hard-won
MicroVM gotchas. The chat spine (SPA → API → SQS → worker → poll, on Cognito,
DynamoDB, S3 and CloudFront) comes from the earlier Bedrock Agents version of
this repo.

## Architecture

    01-sandbox/          MicroVM image: Terraform + image/ (Dockerfile, server.py, kernel.py)
    02-core/             Backend Terraform + code/ (handler, conversations, users, worker, sandbox)
    03-webapp/           Vanilla-JS SPA, uploaded by apply.sh (no Terraform)

### Request flow

1. `POST /conversations/{id}/queries` → the API Lambda writes `question.txt`,
   creates a `QUERY#` record (`pending`) and enqueues SQS.
2. The worker marks the query `processing`, sets `trace_s3_key` right away,
   and runs the **Converse tool loop**. It rewrites `trace.json` after every
   step, which is how the browser shows live progress while it polls.
3. Tools run against the conversation's sandbox. `sandbox.ensure()` launches
   one on first use and records it on the `CONV#` item.
4. When the loop ends, the worker writes `answer.txt` and the final
   `trace.json`, and stores `artifacts` (the staged files) on the query. The
   API re-signs those as short-lived URLs on every poll.

### Why a Converse loop, not Bedrock Agents

- **The model must see its own image.** `show_file` returns the PNG as an
  image block, so the model can catch a bad render. Bedrock Agents action
  groups return text only.
- **The worker waits, not the model.** `run_code` blocks in the worker (up to
  4 minutes, then it hands back a job id for `get_result`). The model never
  has to poll from inside its own reasoning.

### The sandbox (01-sandbox/image)

- `server.py` is the stdlib supervisor. It serves `/execute` (returns a job
  id), `/result/<id>`, `/file?path=` and `/state` on 8080, and the lifecycle
  hooks on 8081. Endpoint tokens are scoped to 8080.
- `kernel.py` is the persistent Python session: one `exec` namespace for the
  whole life of the VM.

## Rules That Are Load-Bearing

- **The kernel takes the protocol descriptors before anything else runs.** It
  dups fds 0 and 1 for the JSON-lines protocol, then points 0/1/2 at
  `/dev/null`, or at a capture file while a cell runs. Without that, a cell's
  `print`, a subprocess, or `input()` would corrupt or swallow protocol lines.
  Never print to real stdout in `kernel.py`.
- **Catch `BaseException` around a cell.** `exit()` inside a cell must fail
  that cell, not end the session.
- **numpy and matplotlib are imported before readiness is reported**, so they
  sit in the snapshot's memory. Moving those imports after the ready line
  makes every first plot cold. Anything imported there is paid for once, at
  image build.
- **Nothing per-session goes into the snapshot.** The session nonce and VM id
  are created in the `/run` hook, after restore.
- **The sandbox role has no policies, on purpose.** Model-written code runs
  there, so anything that role can do, any prompt can do. Files leave the VM
  through the worker (`/file` → S3 under the worker's role). Do not give the
  guest S3 access to "simplify" this.
- **`show_file` on the same path replaces the attachment.** The system prompt
  tells the model to fix a bad render by saving to the same path and showing
  it again. Appending instead would show the user every draft.
- **The token budget is applied at read time.** `users.token_limit()` returns
  `TOKEN_LIMIT_DEFAULT` (1M) unless the usage record has a hand-set
  `token_limit_override`. The legacy `token_limit` attribute is ignored on
  purpose: stored per record, it froze old defaults in place.
- **Image names hash the source files, not the zip.** Every new image name is
  a new image with a one-week minimum storage charge, and zip entries carry
  mtimes that change on every checkout.

## MicroVM Gotchas (inherited, all confirmed)

- `iam:PassRole` on the sandbox role is required to launch with an execution
  role, and the MicroVMs least-privilege example omits it. Do not add an
  `iam:PassedToService` condition: the service never populates that key, so
  the grant silently stops working.
- Any role Lambda assumes (build role, sandbox role) needs `sts:TagSession`
  alongside `sts:AssumeRole`.
- The Lambda runtime's bundled boto3 predates `lambda-microvms`. Both
  functions use the layer that `apply.sh` vendors (`dist/boto3-layer.zip`).
- While a suspended VM is restoring, its endpoint answers 502/503/504.
  `sandbox._request` retries those; do not treat them as failures.
- `GetMicrovm` never wakes a VM. Any request to the endpoint does.
- The base image's `dnf` is microdnf. Do not add `coreutils` to the
  Dockerfile: it conflicts with `coreutils-single`.
- MicroVMs are not owned by Terraform. `destroy.sh` terminates every VM
  launched from the image before destroying anything.

## Model

`bedrock-config.sh` sets `BEDROCK_MODEL_ID`; the default is
`us.anthropic.claude-sonnet-4-6`. On the author's account, Sonnet 5 is listed
ACTIVE in `list-inference-profiles` but Converse refuses it with
AccessDenied. `check_env.sh` probes with a real Converse call for that
reason. The model must support tool use and image input.

## Testing Without Deploying

- `01-sandbox/image` builds locally on
  `public.ecr.aws/amazonlinux/amazonlinux:2023-minimal` (same OS, microdnf and
  Python 3.9 as the MicroVM base) by swapping the `FROM` line. Run it and
  drive it with curl on :8080.
- `worker.Run.converse` can run against real Bedrock with
  `sandbox.ensure`/`_token` patched to return
  `{"endpoint": "http://<container>:8080"}`. The client accepts an explicit
  scheme for exactly this reason.
- `validate.sh` (run by `apply.sh`) smoke-tests a real VM: render, fetch,
  suspend, resume.

## Code Commenting Standards

See the workspace-root `.claude/CLAUDE.md`: comment the *why*, not the *what*.
