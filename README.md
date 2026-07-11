# Cloud Ops Copilot (`aws-agent-ops`)

A demo of **Amazon Bedrock Agents**: a chat assistant that inspects and operates
your AWS account in plain English, and *shows its work* — every reasoning step
and tool call is rendered in a collapsible trace.

> "What's running and what's it costing me?" → the agent calls `list_ec2_instances`
> **then** `get_month_to_date_cost`, correlates them, and answers.
> "Stop the dev box." → it confirms the exact instance, then calls
> `control_ec2_instance`.

It was forked from the `aws-ask-mike` app: the entire async spine (SPA →
API → SQS → worker → poll, on Cognito + DynamoDB + S3 + CloudFront) is reused
verbatim. Only the worker's brain changed — from a retrieval pipeline to a single
`invoke_agent` call. **There is no corpus, no embeddings, no vector search.**

---

## What it showcases

This demo is built to exercise the three things a Bedrock Agent does that a plain
chatbot cannot:

1. **Tool use** — the agent decides which of its tools to call, and calls them.
2. **Multi-step orchestration** — it chains tool calls to answer one question.
3. **Memory** — short-term (this conversation) via `sessionId` and long-term
   (across conversations) via `memoryId`.

The **trace viewer** (expand any answer) is the point of the demo: you watch the
agent decide → call a tool → read the result → answer.

## The tools (the agent's hands)

| Tool | AWS API | Access |
|---|---|---|
| `list_lambda_functions` | `lambda:ListFunctions` | read |
| `list_api_gateways` | `apigateway:GET` | read |
| `list_dynamodb_tables` | `dynamodb:ListTables` / `DescribeTable` | read |
| `list_s3_buckets` | `s3:ListAllMyBuckets` / `GetBucketLocation` | read |
| `get_month_to_date_cost` | `ce:GetCostAndUsage` | read |
| `update_lambda_config` | `lambda:GetFunctionConfiguration` / `UpdateFunctionConfiguration` | **mutate** |

Each tool is a separate Lambda under its **own least-privilege IAM role** — the
agent can never do more than a tool is permitted to. `update_lambda_config` is
the only tool that can change anything, and only a function's memory/timeout.

## Architecture

```
Browser (SPA)
   │  POST /conversations/{id}/queries        GET .../queries/{qid} (poll 2s)
   ▼                                                    ▲
API Lambda ──► SQS ──► Worker Lambda ── invoke_agent ──►│
                                   │                     │
                                   ▼                     │
                         Bedrock Agent ── tools ──► tool Lambdas ──► Lambda / API GW / DynamoDB / S3 / Cost Explorer
                         (memory: session + long-term)
   S3: question / answer / trace.json     DynamoDB: conversations, queries, usage
```

## Deploy

Prereqs: AWS creds, Terraform ≥ 1.5, and the agent's foundation model **enabled**
in Bedrock (`bedrock-config.sh` sets `BEDROCK_MODEL_ID`; it must support Agents).

```bash
./apply.sh      # deploy everything
./destroy.sh    # tear it all down
```

`apply.sh` runs `check_env.sh` first (tooling + creds + model-access probe), then
Terraform (`01-core`), then uploads the SPA (`02-webapp`).

Optional env vars: `AWS_AGENTOPS_GOOGLE_CLIENT_ID` / `_SECRET` (Google sign-in via
Cognito), `AWS_AGENTOPS_CUSTOM_DOMAIN` (custom domain instead of the CloudFront
default).

## Try it (demo script)

Sign in, then:

1. **"Give me a complete inventory of my serverless resources and my month-to-date cost."**
   → the trace shows the agent fan out across `list_lambda_functions`,
   `list_api_gateways`, `list_dynamodb_tables`, `list_s3_buckets`, and
   `get_month_to_date_cost`, then assemble one answer. The orchestration money shot.
2. **"List my Lambda functions with their memory and runtime."** → `list_lambda_functions`.
3. **"Break down my spend by service."** → `get_month_to_date_cost(group_by_service=true)`.
4. **"Bump the memory on `my-func` to 512 MB."** → the agent restates the function
   and the before/after values and asks you to confirm before calling
   `update_lambda_config`. Then **"put it back"** — it remembers the old value.
5. **"Remember my monthly budget is $50 — am I over it?"** → recalls the $50 and
   compares to the cost tool (reliable within one conversation; cross-conversation
   recall needs the prior session to summarize — see below).

Expand the trace under any answer to see exactly which tools ran, with what args.

## Cost note

No vector store, no always-on infra — everything is serverless and scales to
zero. You pay per agent invocation (Bedrock model tokens) and trivial Lambda /
API / DynamoDB usage. `./destroy.sh` leaves nothing running.

## Layout

```
01-core/          Terraform + Lambda code
  agent.tf        Bedrock Agent, action groups, alias, memory, agent role
  tools.tf        4 tool Lambdas + least-privilege roles + invoke permission
  lambdas.tf      API + worker Lambdas
  iam.tf          shared API/worker role (InvokeAgent, DynamoDB, S3, SQS)
  code/           handler / conversations / users / worker / tool_*.py
02-webapp/        vanilla-JS SPA (chat + collapsible agent-trace viewer)
```
