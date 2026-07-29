# Triggers — four ways to start a run

[繁體中文](TRIGGERS.zh-TW.md) · [Architecture](ARCHITECTURE.md) · [Test results](TEST_RESULTS.md)

All four triggers converge on the same single entry point: the
`llmops-start-pipeline` Lambda. It mints the `run_id`, seeds the S3 manifest
(the single source of truth every stage reads), records the run in DynamoDB,
emits `PipelineStarted`, and starts the Step Functions execution. A trigger
payload's `params` override the manifest defaults (dataset, sample_count,
gates, instance types, `max_iterations`, models).

Provisioned by `deploy/08_triggers.py` (idempotent, `--dry-run` supported).
Live status as of Phase 5:

| Trigger | Mechanism | Auth | Verified |
|---|---|---|---|
| EventBridge Scheduler | nightly cron, **DISABLED by default** | scheduler role | created |
| Webhook | API Gateway HTTP API `POST /webhook` | HMAC-SHA256 | live: bad sig → 403, good sig → 202 + run started |
| Admin API | `POST /runs` on the same HTTP API | AWS_IAM | route live |
| GitHub Actions | `workflow_dispatch` → OIDC → Lambda invoke | OIDC role | workflow in repo (`.github/workflows/run-pipeline.yml`); needs one-time OIDC role + `AWS_OIDC_ROLE_ARN` secret |

## 1. EventBridge Scheduler (nightly cron)

Schedule `llmops-nightly`: `cron(0 3 * * ? *)` (03:00 UTC nightly, flexible
15-minute window), invoking `llmops-start-pipeline` with
`{"trigger_source": "scheduler"}`. It is created **DISABLED** — a platform
whose runs cost real money should not start billing itself the moment it is
provisioned.

Enable it when you actually want nightly runs:

```bash
# via the deploy script (idempotent create-or-update, sets state ENABLED)
python deploy/08_triggers.py --region us-east-1 --enable-schedule

# or directly
aws scheduler update-schedule --name llmops-nightly --state ENABLED \
  --schedule-expression "cron(0 3 * * ? *)" ...
```

The scheduler assumes a dedicated role (`llmops-scheduler-invoke`) whose only
permission is `lambda:InvokeFunction` on `llmops-start-pipeline`.

## 2. Webhook (HMAC-SHA256)

`POST /webhook` on the `llmops-triggers` HTTP API (endpoint URL published to
SSM `/llmops/triggers/api_endpoint`). The route itself has no API Gateway
auth — verification happens inside the `llmops-webhook` Lambda: the caller
must send `X-Signature-256: sha256=<hmac-sha256-hex of the raw body>`, keyed
by the shared secret in Secrets Manager (`llmops/webhook`, auto-created by
`08_triggers.py`).

Computing and sending a signed request:

```bash
ENDPOINT=$(aws ssm get-parameter --name /llmops/triggers/api_endpoint \
  --query Parameter.Value --output text)
SECRET=$(aws secretsmanager get-secret-value --secret-id llmops/webhook \
  --query SecretString --output text)

BODY='{"params": {"task_count": 24, "note": "webhook demo"}}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')

curl -s -X POST "$ENDPOINT/webhook" \
  -H "Content-Type: application/json" \
  -H "X-Signature-256: sha256=$SIG" \
  -d "$BODY"
# → 202 {"run_id": "run-...", "manifest_uri": "s3://.../manifest.json"}
```

Behavior (live-verified in Phase 5):

- **Invalid or missing signature → 403** `{"error": "forbidden"}`. Constant-time
  comparison (`hmac.compare_digest`); the response leaks no information about
  *why* it was rejected.
- **Valid signature → 202** with the started run's `run_id` and `manifest_uri`.
  The body's `params` are forwarded to start-pipeline with
  `trigger_source: "webhook"`.
- Valid signature but malformed JSON body → 400.

Note the HMAC is computed over the **raw request body** — re-serializing the
JSON (key order, whitespace) will change the signature and get you a 403.

## 3. Admin API (`POST /runs`, AWS_IAM)

`POST /runs` on the same HTTP API routes directly to `llmops-start-pipeline`,
with **AWS_IAM** authorization — callers must SigV4-sign the request with
credentials that allow `execute-api:Invoke`. Meant for the ops console and
operators; [awscurl](https://github.com/okigan/awscurl) is the easiest CLI:

```bash
ENDPOINT=$(aws ssm get-parameter --name /llmops/triggers/api_endpoint \
  --query Parameter.Value --output text)

awscurl --service execute-api --region us-east-1 \
  -X POST "$ENDPOINT/runs" \
  -H "Content-Type: application/json" \
  -d '{"trigger_source": "admin-api",
       "params": {"task_count": 24, "sample_count": 2000}}'
# → {"run_id": "run-...", "manifest_uri": "s3://...", "execution_arn": "..."}
```

An unsigned `curl` to the same route is rejected by API Gateway before the
Lambda ever runs.

## 4. GitHub Actions (`workflow_dispatch`, OIDC)

`.github/workflows/run-pipeline.yml` starts a run from the GitHub UI (or
`gh workflow run`) with **no long-lived AWS keys** — the job assumes an IAM
role via GitHub's OIDC provider.

One-time setup:

1. Create (or reuse) the GitHub OIDC identity provider in the AWS account
   (`token.actions.githubusercontent.com`).
2. Create an IAM role trusting that provider **scoped to this repository**,
   with a single permission: `lambda:InvokeFunction` on
   `llmops-start-pipeline`.
3. Store the role's ARN in the repo secret **`AWS_OIDC_ROLE_ARN`** — the
   account ID never appears in the workflow file (this is a public repo).

Running it:

```bash
gh workflow run run-pipeline.yml \
  -f task_count=24 -f sample_count=2000 -f note="release candidate"
```

Semantics are **fire-and-monitor, not fire-and-wait**: a Step Functions run
outlives any GitHub job, so the workflow invokes start-pipeline with
`trigger_source: "github-actions"`, prints the `run_id` to the job summary,
and exits green. Watch progress in the ops console or the Step Functions
console — a green Actions run means "started", not "passed".

Status: the workflow ships in this repo (`.github/workflows/run-pipeline.yml`).
One-time setup remains on the AWS side: create the OIDC role and store its ARN
in the `AWS_OIDC_ROLE_ARN` repo secret (steps above).
