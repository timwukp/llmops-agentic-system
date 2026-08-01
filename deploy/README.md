# deploy/ — provisioning scripts (run in numbered order)

Idempotent Python scripts that stand up the llmops-agentic-system foundation on your
AWS account. Every script is safe to re-run, supports `--dry-run` (prints a plan/diff,
writes nothing), tags everything `project=llmops-agentic-system`, and publishes its
outputs to SSM Parameter Store under `/llmops/*` so later steps need no copy-pasting.

## Least-privilege statement

No `*FullAccess` managed policies are used anywhere. Every IAM statement in
`deploy/iam/*.json` is scoped by resource ARN pattern (`llmops-*` names, specific
bucket prefixes, the specific event bus/topic/table/state machine) and/or condition,
except for the handful of actions AWS does not support resource-level permissions for
(e.g. `states:SendTaskSuccess` on task tokens, `ec2:CreateNetworkInterface` for Lambda
VPC ENIs, `sagemaker:List*`, `ecr:GetAuthorizationToken`) — each such statement carries
a comment explaining why. The policy documents contain only `<ACCOUNT_ID>` /
`<REGION>` / `<DATA_BUCKET>` placeholders; real account ids are substituted at deploy
time from `sts get-caller-identity` and are never committed to the repo.

## Run order

| # | Script | What it creates | When |
|---|--------|-----------------|------|
| 01 | `01_iam.py` | 6 roles: `llmops-harness-execution`, `llmops-sagemaker-execution`, `llmops-lambda-{driver,start,resume,webhook}` + inline policies; SSM `/llmops/iam/*` | always, first |
| 02 | `02_network.py` | dedicated VPC, 2 private subnets, SGs, gateway + interface VPC endpoints; SSM `/llmops/network/*` | **prod only** (dev harnesses use PUBLIC network mode) |
| 03 | `03_storage.py` | S3 data bucket (versioned, SSE-S3, public-access-block, `runs/` 90-day lifecycle), DDB `llmops-pipeline-runs` + `llmops-stage-events`, EventBridge bus `llmops-pipeline`, SNS `llmops-escalations`; SSM `/llmops/storage/*` | always |
| 04 | `04_wire_memory.py` | shared BYO AgentCore Memory (SEMANTIC+EPISODIC), attach to every harness, per-Memory IAM grant; SSM `/llmops/memory/*` | after 05 creates harnesses (re-run to attach new ones) |
| 05 | `05_harnesses.py` | all 7 harnesses from `agents/*/harness.json`; injects `OTEL_TRACES_SAMPLER=always_on`. `--prod` reads `harness.prod.json`, which **no agent has yet** (VPC-mode variants are unbuilt and need the S3 skill mirror first); SSM `/llmops/harness/*` | after 01/03 (and 02 for prod) |
| 06 | `06_observability.py` | log/trace deliveries per harness (targets the auto-created runtime ARN); `--evals` attaches Builtin online evaluation configs (needs `llmops-eval-execution` role) | after 05 |
| 07 | `07_lambdas.py` | 4 spine Lambdas (driver/start/resume/webhook, contracts vendored), Step Functions `llmops-pipeline`, SageMaker job-state EventBridge rule | after 05 |
| 08 | `08_triggers.py` | EventBridge Scheduler (nightly, DISABLED), webhook HMAC secret, HTTP API (`POST /webhook`, `POST /runs` IAM); SSM `/llmops/triggers/*` | after 07; `--enable-schedule` for nightly |
| — | ops console | deployed from [bedrock-agentcore-agent-ops-console](https://github.com/timwukp/bedrock-agentcore-agent-ops-console) `deploy/deploy.sh` with `UI_HARNESS`/`QA_BUCKET` env pointing at this platform | last |

```bash
.venv/bin/python deploy/01_iam.py     --region us-east-1 [--dry-run]
.venv/bin/python deploy/02_network.py --region us-east-1 [--dry-run]   # prod only
.venv/bin/python deploy/03_storage.py --region us-east-1 [--dry-run]
```

Offline dry-run (no AWS credentials at all): add `--account-id 123456789012` to 01/03.

`preflight.py` and `validate_config.py` can be run at any point to sanity-check the
environment and harness configs without touching AWS state.

## Teardown notes

Reverse order. Nothing here bills meaningfully while idle **except** the interface VPC
endpoints from step 02 (~$0.01/hr each) — drop them anytime with
`02_network.py --region us-east-1 --destroy` (gateway endpoints, subnets and the VPC
itself are free and are kept).

- **SageMaker endpoints** are the real cost risk: they are created/deleted by the
  pipeline itself, so after any aborted run check `aws sagemaker list-endpoints` for
  `llmops-*` leftovers and delete them.
- **S3**: the bucket is versioned; to delete it you must purge all object *versions*
  (`aws s3api delete-objects` over `list-object-versions`, or a lifecycle rule), then
  `aws s3api delete-bucket`. `runs/` self-expires after 90 days either way.
- **DDB tables / event bus / SNS topic**: on-demand billing, ~zero at rest; delete with
  `aws dynamodb delete-table`, `aws events delete-event-bus --name llmops-pipeline`,
  `aws sns delete-topic`.
- **IAM roles**: free; to remove, delete the inline policy `llmops-permissions` from
  each `llmops-*` role, then the role. Delete the SSM parameters under `/llmops/`.
- **AgentCore**: harnesses, the shared Memory, and any auto-created `harness_*` managed
  memories are deleted via their own scripts/console (steps 04-05 docs).
