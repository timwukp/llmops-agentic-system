# LLMOps Admin Console

Operator dashboard for the llmops-agentic-system pipeline: one Lambda serves both the
dashboard HTML (`GET /`) and the JSON API (`GET/POST /api/*`) behind an HTTP API Gateway.

**Design credit:** architecture, auth model, security headers, observability/evaluations/
optimizations code, and the deploy script are ported from
[bedrock-agentcore-agent-ops-console](https://github.com/timwukp/bedrock-agentcore-agent-ops-console).
One deliberate departure: the frontend ships as `frontend.html` inside the zip and is read
once at cold start (no giant inline HTML string in the handler).

## Tabs

Nine, in the order they appear in the nav. This table listed five and described "the 6
`llmops_*` harnesses"; both had drifted (three tabs were added and the fleet is seven).
`tests/test_console_routes.py` derives the tab count from `frontend.html` now, so the
next added tab fails the suite rather than quietly making this list wrong again — which
is how the Introduction row below got written on the same commit as the tab itself.

| Tab | What it shows |
|---|---|
| Introduction | A narrated five-minute walkthrough of the problem, the build and the measured cost, in an iframe (`GET /intro`) with narration in five languages (`GET /intro/audio/<lang>/<scene>.mp3`, 35 clips bundled in the zip). **The default landing tab** for a first-time visitor; a returning operator still lands on whatever tab they left |
| Architecture | The generated diagrams (`docs/architecture-*.svg`), served from the same Lambda so the picture cannot lag the deployment |
| Tasks | **The customer-facing plane** — one consultation thread per engagement with `llmops_orchestrator`: chat, presigned `customer-data/` upload, priced plan, KMS-signed acceptance, lifecycle strip |
| Pipeline | Animated 9-stage flow (Step Functions `llmops-pipeline`) with remediation-loop arrow, execution picker, run detail (gates / metrics / evidence / training job), Start Run, and the verdicts panel (delivered / parked / never delivered) |
| Fleet | The 7 `llmops_*` harnesses: status, model, skills, limits |
| Observability | Per-harness AgentCore service metrics, daily bar chart, token tile, SageMaker training jobs + student endpoints |
| Evaluations | Online eval configs with score gauges, batch evaluations, insights reports |
| Optimizations | AWS-native system-prompt recommendations + Bedrock-drafted prompts (human-review-then-apply via UpdateHarness) |
| Cost | Estimates vs reconciled actuals, the $20,000 reference, approval queue (approver group, never self-approve), `finops-run` dispatch |

Route shape: **30 handlers** — 13 public GETs, 3 session POSTs, 14 authenticated POSTs.
See [ARCHITECTURE.md §13](../../docs/ARCHITECTURE.md#13-the-admin-console--one-lambda-three-planes-with-different-rules)
for why the three planes carry different rules.

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
| `TASKS_TABLE` | `llmops-tasks` | Consultation threads (PK `id`, `task-` prefix) |
| `ESTIMATES_TABLE` | `llmops-cost-estimates` | Cost estimates (PK `id`) |
| `ACTUALS_TABLE` | `llmops-cost-actuals` | Reconciled actuals (PK `project`, SK `sk`) |
| `FINOPS_FN` | `llmops-finops-reconcile` | Auditor dispatch target |
| `PROJECT` | `llmops-agentic-system` | Cost attribution key |
| `APPROVAL_LIMIT_USD` | `20000` | Single-run reference — **server-side**; a gate the client enforces is a gate the client can skip. `deploy.sh` sets it from `cost_model.DEFAULT_SINGLE_RUN_LIMIT_USD`; it set neither limit until 2026-08-02, so the live function read `null` and fell back to the code default |
| `CUMULATIVE_LIMIT_USD` | `20000` | Period reference, checked alongside the single-run one |
| `BUDGET_MODE` | `advisory` | `advisory` names an over-budget dispatch and records the estimate; `blocking` refuses it |
| `APPROVER_GROUP` | `llmops-approver` | Cognito group that may decide approvals (never the requester) |
| `DS_GROUP` | `llmops-datascience` | Cognito group that may create/chat tasks and mint upload URLs |
| `APPROVAL_KEY` | `alias/llmops-approval` | KMS key signing approvals and plan acceptances (hash-chained) |
| `LLMOPS_SNS_TOPIC` | (empty) | Escalation topic for `page_human`. Had zero subscribers until 2026-08-02; now one confirmed email recipient (measured 2026-08-10: 15 published / 11 delivered / 0 failed, the 4 undelivered all predating the confirmation). Subscribe with `deploy/03_storage.py --escalation-email <addr>`; `ensure_topic` prints `NO SUBSCRIBERS` if it ever goes back to zero |
| `ALLOWED_ORIGIN` | (empty) | Leave empty: same-origin. Setting it widens CORS |
| `REFRESH_COOKIE_MAX_AGE_S` | 30 days | httpOnly refresh cookie lifetime |

Account ID is resolved at runtime from STS — nothing account-specific is hardcoded.

## End-to-end: what a customer engagement actually traverses

The console is one end of a chain that runs all the way to a trained student model. Every
step below is a real deployed component, in order:

1. **Consult** — customer (or operator) opens a thread on the Tasks tab. `POST /api/tasks`
   → task row → the task-chat worker invokes `llmops_orchestrator` through the harness
   driver, streaming the reply. The orchestrator's consult protocol opens at DATA
   DISCOVERY, which is why `llm-data-preparation` is mounted on it.
2. **Hand over data** — `POST /api/data-upload-url` mints a **presigned** PUT under
   `customer-data/<task_id>/`. The key is always server-chosen; the browser uploads
   directly to S3 (no 6 MB API Gateway cap, no Lambda timeout) and the thread auto-posts
   the resulting URI so the upload is visible *to the agent* rather than a silent
   side-effect. The pipeline's own role gets `customer-data` **read-only**.
3. **Priced plan** — the orchestrator emits a plan with a `$` figure derived from the rate
   card injected into its prompt. Acceptance is KMS-signed and hash-chained, so it is
   evidence rather than a UI state.
4. **Dispatch** — accepted plan → `launch_run` → `llmops-start-pipeline`, which seeds
   `s3://<bucket>/runs/<run_id>/manifest.json` and starts the Step Functions execution.
   Over-budget dispatches are named by the gate; `advisory` records and proceeds.
5. **Run** — the deterministic spine drives 8 harness-task states (data-prep → finetune →
   eval → deploy → monitor), each a `waitForTaskToken` driver invocation. Long training
   jobs release the session and resume via EventBridge. Gate failure loops through
   remediation ≤3 times, then escalates.
6. **Watch** — the Pipeline and Observability tabs read the run's stage events (two bounded
   queries: events `sk < "A"`, parked verdicts `sk begins_with "directive#"`) plus
   AgentCore/CloudWatch metrics. Both records — run and originating task — are closed on
   both the success and failure paths.
7. **Reconcile** — after the run (Cost Explorer lags ~24 h), `POST /api/finops-run` dispatches
   `llmops_finops`, the read-only auditor, which compares actuals to the estimate using
   `cost_model.py` as the single canonical arithmetic. The Cost tab shows both sides.

The auditor is outside the state machine on purpose, and its IAM is read-only on billing:
the thing that measures spend is not the thing that authorises it.
