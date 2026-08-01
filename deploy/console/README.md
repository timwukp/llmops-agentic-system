# LLMOps Admin Console

Operator dashboard for the llmops-agentic-system pipeline: one Lambda serves both the
dashboard HTML (`GET /`) and the JSON API (`GET/POST /api/*`) behind an HTTP API Gateway.

**Design credit:** architecture, auth model, security headers, observability/evaluations/
optimizations code, and the deploy script are ported from
[bedrock-agentcore-agent-ops-console](https://github.com/timwukp/bedrock-agentcore-agent-ops-console).
One deliberate departure: the frontend ships as `frontend.html` inside the zip and is read
once at cold start (no giant inline HTML string in the handler).

## Tabs

| Tab | What it shows |
|---|---|
| Pipeline | Animated 9-stage flow (Step Functions `llmops-pipeline`) with remediation-loop arrow, execution picker, run detail (gates / metrics / evidence / training job), Start Run |
| Fleet | The 6 `llmops_*` harnesses: status, model, skills, limits |
| Observability | Per-harness AgentCore service metrics, daily bar chart, token tile, SageMaker training jobs + student endpoints |
| Evaluations | Online eval configs with score gauges, batch evaluations, insights reports |
| Optimizations | AWS-native system-prompt recommendations + Bedrock-drafted prompts (human-review-then-apply via UpdateHarness) |

## Auth model (ported)

- All `GET` routes are public read-only (demo-friendly; nothing sensitive is returned).
- Every `POST` except `/api/login`, `/api/refresh` and `/api/refresh/revoke` requires a
  Cognito access token (`Authorization: Bearer ...`), validated server-side via
  `cognito-idp GetUser`. The three exceptions are unauthenticated *because* they are the
  routes that establish, restore or end a session — requiring a live token to recover
  from having lost one is circular.
- The browser keeps the access token in memory only — never in localStorage.
- Session survival across a page reload comes from an **httpOnly refresh cookie**
  (`llmops_rt`, `Path=/api/refresh`, `Secure`, `SameSite=Strict`), set by `/api/login`
  and exchanged for a new access token by `/api/refresh`. Page script cannot read it, so
  an XSS bug costs one 8-hour access token rather than 30 days of re-issue, and the
  narrow `Path` means the browser never attaches it to any other route.
- `/api/refresh/revoke` (sign-out) calls Cognito `RevokeToken` and expires the cookie.
  An *expired access token* must never take this path: it clears the in-memory token
  only, so the next action silently re-mints one. Conflating the two would recreate the
  re-login-on-every-refresh behaviour this design fixes.

## Deploy

```bash
cd deploy/console
REGION=us-east-1 ./deploy.sh
```

Idempotent: re-run for code updates. Creates (llmops-prefixed, no collision with any
existing `agent-cicd-admin` stack):

- Lambda `llmops-admin` (Python 3.12, zip = `lambda_function.py` + `frontend.html` + vendored boto3)
- IAM role `LlmopsAdminLambdaRole` with `iam-policy.json`
- DynamoDB `LlmopsAdminRuns` (console state: optimization drafts only)
- Cognito pool `llmops-admin-pool` (admin-create-only, PLUS tier threat protection,
  20-char min password); admin credentials in Secrets Manager `llmops-admin/dashboard-login`
- HTTP API `llmops-admin-api` with stage throttling

## Env contract (Lambda)

| Var | Default | Purpose |
|---|---|---|
| `CONSOLE_TABLE` | `LlmopsAdminRuns` | Console state (opt- drafts) |
| `RUNS_TABLE` | `llmops-pipeline-runs` | Pipeline runs (read) |
| `EVENTS_TABLE` | `llmops-stage-events` | Stage events (read, Query by run_id) |
| `STATE_MACHINE` | `llmops-pipeline` | Step Functions state machine name |
| `START_FN` | `llmops-start-pipeline` | Start-run trigger Lambda |
| `DATA_BUCKET` | (empty) | S3 data bucket; falls back to SSM `/llmops/storage/bucket` |
| `COGNITO_POOL_ID` / `COGNITO_CLIENT_ID` | set by deploy.sh | POST auth |
| `JUDGE_MODEL` | `global.anthropic.claude-opus-5` | Bedrock model for prompt drafts |
| `SPANS_SINCE` | `2026-07-28T12:00:00Z` | Sessions before OTEL always_on can never score — excluded from batch eval / insights |
| `OPTIMIZE_HARNESS` | `llmops_orchestrator` | Default optimization target |

Account ID is resolved at runtime from STS — nothing account-specific is hardcoded.
