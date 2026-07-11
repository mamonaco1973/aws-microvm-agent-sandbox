# CLAUDE.md

Guidance for working in **aws-agent-ops** (product name: **Cloud Ops Copilot**).

## What This App Is

A ChatGPT-style assistant backed by an **Amazon Bedrock Agent** that inventories
and tunes the serverless resources in an AWS account through tools — "give me a
full inventory of my Lambdas, APIs, tables, and buckets and what they cost",
"bump my-func to 512 MB" (with confirmation). It is a demo
of **Bedrock Agents (Option 2)**: tool use, multi-step orchestration, and managed
session + long-term memory. It was forked from the `aws-ask-mike` app and
keeps that app's async spine; only the "brain" changed.

There is **no retrieval** here — no corpus, embeddings, or vector search. Retrieval,
prompt assembly, tool orchestration, and conversation memory all live inside the
agent. The worker just calls `invoke_agent` and stores the result.

## Architecture

    01-core/           # Backend: Terraform + Python Lambda source
      code/            # Lambda source (API, worker, tool handlers)
    02-webapp/         # Frontend: vanilla-JS SPA on S3 + CloudFront

### Request flow

1. User asks a question → `POST /conversations/{id}/queries` → API Lambda writes
   `question.txt` to S3, creates a `QUERY#` record (`pending`), enqueues SQS.
2. Worker Lambda (SQS trigger) calls **`invoke_agent`** with
   `sessionId=conv_id` (short-term memory), `memoryId=user_id` (long-term
   memory), `enableTrace=True`.
3. It streams the completion → answer text + a **trace** (reasoning + tool
   calls), sums the agent's token usage, writes `answer.txt` + `trace.json`,
   marks the query `complete`.
4. Frontend polls `GET …/queries/{query_id}` every 2s; on completion renders the
   answer with a **collapsible trace viewer** (the "sources" widget,
   repurposed).

### The agent (agent.tf)

- `aws_bedrockagent_agent` — foundation model + `instruction` (system prompt) +
  `memory_configuration` (SESSION_SUMMARY, 30-day) → cross-session memory.
- Six **action groups**, one per tool, each pointing at its own Lambda:
  `list_lambdas`, `list_apis`, `list_tables`, `list_buckets`, `get_costs`,
  `control_lambda`.
- `aws_bedrockagent_agent_alias` "live" — the stable endpoint the worker calls;
  `depends_on` all action groups so "prepare" bakes the tools into the version.

### Tools = the agent's hands (tools.tf + code/tool_*.py)

One Lambda per tool, each under its **own least-privilege role** — the agent can
only do what a tool's IAM allows:

- `tool_list_lambdas` — `lambda:ListFunctions` (read)
- `tool_list_apis` — `apigateway:GET` (read; HTTP + REST)
- `tool_list_tables` — `dynamodb:ListTables`/`DescribeTable` (read)
- `tool_list_buckets` — `s3:ListAllMyBuckets`/`GetBucketLocation` (read)
- `tool_get_costs` — `ce:GetCostAndUsage` (read)
- `tool_control_lambda` — `lambda:Get/UpdateFunctionConfiguration` (**only** mutator)

Bedrock invokes them via a resource-based `aws_lambda_permission` scoped to the
agent's ARN. `tool_common.py` hides the Bedrock action-group event/response
envelope.

### Lambda files (code/)

- `handler.py`       — API router (register, usage, conversations, queries)
- `conversations.py` — conversation + query CRUD; token budget; `trace` hydration
- `users.py`         — registration (USER_CAP) + `GET /usage`
- `worker.py`        — SQS worker: `invoke_agent` + trace capture + persistence
- `tool_*.py`        — the four action-group handlers
- `tool_common.py`   — shared event-parse / response-format helpers

### Data model (DynamoDB single-table)

- `pk=USER#<id>`, `sk=USER#USAGE`        — tokens_used, token_limit
- `pk=USER#<id>`, `sk=CONV#<id>`         — title, timestamps
- `pk=USER#<id>`, `sk=QUERY#<conv>#<id>` — status, S3 pointers (`answer_s3_key`,
  `trace_s3_key`), tokens_used

## Deployment

```bash
./apply.sh      # full deploy
./destroy.sh    # tear down
./check_env.sh  # validate tooling, creds, Bedrock model access
```

The agent's foundation model comes from `bedrock-config.sh` (`BEDROCK_MODEL_ID`),
passed to Terraform as `agent_foundation_model` and probed by `check_env.sh`.

## Gotchas that will bite

- **Model access + Agents support** — the `agent_foundation_model` must be
  enabled for your account AND support Bedrock Agents. If apply fails on the
  agent, switch the model in `bedrock-config.sh`.
- **Provider version** — Bedrock Agent + `memory_configuration` need AWS provider
  ≥ 5.70 (pinned in `versions.tf`).
- **Alias vs. action groups** — the alias must `depends_on` every action group or
  it ships an agent with no tools. If you add a tool, re-apply.
- **Long-term memory** — session summaries are written when a session ends /
  idles out; cross-session recall isn't instant within one open conversation.
- **Trace parsing is best-effort** — the agent trace schema is deep; `worker.py`
  parses defensively and must never fail a query on a trace miss.

## Code Commenting Standards

See the workspace-root `.claude/CLAUDE.md`: comment the *why*, not the *what*.
