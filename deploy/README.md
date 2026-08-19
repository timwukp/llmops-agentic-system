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
| 02 | `02_network.py` | dedicated VPC, 2 private subnets, SGs, gateway endpoints, SSM `/llmops/network/*` — all free. The 11 **billed** interface endpoints are skipped unless something routes through them (`--force-unused-endpoints` overrides) | **prod only** (dev harnesses use PUBLIC network mode) |
| 03 | `03_storage.py` | S3 data bucket (versioned, SSE-S3, public-access-block, `runs/` 90-day lifecycle), DDB `llmops-pipeline-runs` + `llmops-stage-events`, EventBridge bus `llmops-pipeline`, SNS `llmops-escalations`; SSM `/llmops/storage/*`; canonical code mirrors with byte read-back — `code/distill/` (QLoRA trainer), `code/eval/` (judge prompt, RAFT format, vision mAP scorer), `code/vision/` (detection trainer) | always |
| 04 | `04_wire_memory.py` | shared BYO AgentCore Memory (SEMANTIC+EPISODIC), attach to every harness, per-Memory IAM grant; SSM `/llmops/memory/*` | after 05 creates harnesses (re-run to attach new ones) |
| 05 | `05_harnesses.py` | all 7 harnesses from `agents/*/harness.json`; injects `OTEL_TRACES_SAMPLER=always_on`. `--prod` reads `harness.prod.json`, which **no agent has yet** (VPC-mode variants are unbuilt and need the S3 skill mirror first); SSM `/llmops/harness/*` | after 01/03 (and 02 for prod) |
| 06 | `06_observability.py` | log/trace deliveries per harness (targets the auto-created runtime ARN); `--evals` attaches Builtin online evaluation configs (needs `llmops-eval-execution` role) | after 05 |
| 07 | `07_lambdas.py` | 7 spine Lambdas (driver/start/resume/resurrector/webhook/finops-reconcile/monitor-sweep, contracts vendored), Step Functions `llmops-pipeline`, SageMaker job-state EventBridge rule. `--only` selects any single target incl. `state_machine` / `resume_rule` | after 05 |
| 08 | `08_triggers.py` | EventBridge Scheduler ×3 — nightly DISABLED, `llmops-finops-daily` 09:00 UTC ENABLED, `llmops-monitor-sweep-daily` 08:00 UTC ENABLED — webhook HMAC secret, HTTP API (`POST /webhook`, `POST /runs` IAM); SSM `/llmops/triggers/*` | after 07; `--enable-schedule` for nightly, `--no-finops-schedule` / `--no-sweep-schedule` to ship the daily ones off |
| 09 | `09_retrieval.py` | Bedrock Knowledge Base for RAFT runs: `llmops-kb-service` role, AOSS `llmops-retrieval` collection + knn index, corpus exploded one-object-per-row to `customer-data/kb-corpus/`, KB + S3 data source pinned to that prefix (acceptance sets structurally excluded — the script **refuses** eval keys under it), `--ingest` reconciles document counts; SSM `/llmops/retrieval/*`. **STANDING COST: ~$11.52/day (min 2 OCU) while the collection exists — run `--teardown` when the run's eval is done** | per RAFT run (r6d+), only with a plan that names `retrieval_kb_id`; teardown after eval |
| — | ops console | deployed from [bedrock-agentcore-agent-ops-console](https://github.com/timwukp/bedrock-agentcore-agent-ops-console) `deploy/deploy.sh` with `UI_HARNESS`/`QA_BUCKET` env pointing at this platform | last |

```bash
.venv/bin/python deploy/01_iam.py     --region us-east-1 [--dry-run]
.venv/bin/python deploy/02_network.py --region us-east-1 [--dry-run]   # prod only
.venv/bin/python deploy/03_storage.py --region us-east-1 [--dry-run]
```

Offline dry-run (no AWS credentials at all): add `--account-id 123456789012` to 01/03.

`preflight.py` and `validate_config.py` can be run at any point to sanity-check the
environment and harness configs without touching AWS state.

## Before you deploy (and before you trust a live result): `tools/audit_drift.py`

One **read-only** command that answers "is production running this tree's code?", because
the honest answer has been *no* for weeks at a time and nothing said so. Measured on
2026-08-15, immediately before a live rehearsal: `eval_only` had never been deployed,
`_check_mode_prerequisites` did not exist in the live start Lambda, the driver was still
running a mechanism replaced two PRs earlier, and 3 of 7 prompts plus 4 of 7 Lambdas had
drifted. Every one of those was invisible to the test suite, which reads the working tree.

```bash
.venv/bin/python tools/audit_drift.py --region us-east-1 [--json drift-audit.json]
.venv/bin/python tools/audit_drift.py --region us-east-1 --account-id 123456789012 --offline
```

Five legs — IAM inline policies, the Step Functions definition, harness configs, Lambda
configuration, Lambda **code** — each comparing what the deploy scripts *would send*
against what the control plane holds. The comparators are the deploy scripts' own
(`state_machine_drift`, `harness_config_drift`, `policy_diff`), so there is no second
opinion to drift. Lambda code is compared **per zip member with the first differing line
named**, never by `CodeSha256`: `bundle()` stamps source mtimes into the zip, so a sha
cannot be reproduced from a fresh checkout and comparing it would report permanent drift.

Exit codes are the contract, and `0` is the narrow one:

| Code | Meaning |
|------|---------|
| `0` | every leg compared **and** clean — live matches this tree |
| `1` | drift found |
| `2` | no drift, but at least one leg could **not** be compared (no credentials, AccessDenied, resource absent). Not a pass |
| `3` | usage error |

`--offline` builds every sent side, runs `env_keys_for` + `env_values` for real, refuses
any unresolved `<PLACEHOLDER>`, and makes **no AWS call at all** (it runs in CI's offline
dry-run step). The report always ends by printing what it does *not* check — extra managed
or inline policies, memory wiring, tags, `CodeSha256`, layers/concurrency/VPC, SSM
parameters, EventBridge rules, DynamoDB tables — and it compares the **working tree**, so
run it from a clean `main` checkout; the JSON carries `repo_head` so an answer can be
attributed to a commit.

Deliberately **not** wired into CI: the only credentialed workflow is scoped to
`lambda:InvokeFunction` on one function, and widening that role to read the control plane
would be both a live IAM write and a new standing capability.

## Teardown notes

Reverse order. Nothing here bills meaningfully while idle **except** the interface VPC
endpoints from step 02 — drop them anytime with
`02_network.py --region us-east-1 --destroy` (gateway endpoints, subnets and the VPC
itself are free and are kept).

They bill **$0.01/hr each *per Availability Zone***, not per endpoint: `SubnetIds` creates
one endpoint network interface per subnet and the ENI is the billed unit, so 11 services
across 2 subnets is **22** billed endpoint-hours per hour, ~**$5.28/day**. The script used
to print ~$2.64/day — the one-AZ answer — which is why `02_network.py` now derives the
figure from `len(INTERFACE_SERVICES) × len(subnet_ids)`. It also **skips the 11 interface
endpoints by default**, because nothing in this repo routes through them yet (no
non-`PUBLIC` harness config, no Lambda with `VpcConfig`); everything free is still built,
and `--force-unused-endpoints` overrides.

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
