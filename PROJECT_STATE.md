# PROJECT_STATE.md — deployed resources & progress (redacted)

Persistent memory for humans and agents. Update whenever AWS resources are
created/deleted or a phase gate passes. Never record account IDs or full ARNs.

## Current phase

**v1.2.0 — reachability** (2026-08-02): 21 merged PRs (#17–#38), almost all of one shape —
a component that was deployed, tested, and never reached. The eval gate read a report nothing
wrote; the `llmops-pipeline` bus carried ZERO rules so every escalation published into nothing;
`llmops_monitor` had no task dispatched anywhere; the SUCCESS path had never executed, so every
successful run was a zombie record. Also: a consultation the customer can finish end to end
(KMS-signed plan acceptance, presigned upload, one thread), streaming replies, and the orphan
endpoint re-costed from measured hardware at $36.36/day — 2× the $18 six files claimed — then
deleted.

**v1.1.0 — FinOps** (2026-07-31): cost estimation, the $2000 dual approval gate, and the
7th runtime `llmops_finops`. v1 complete before it (all six phases 2026-07-28 → 2026-07-29).

Next: v2 experiments — code-as-reasoning distillation + augmentation; Kimi K3 teacher A/B
(see docs/CASE_STUDY.md).

## Phase gates

| Phase | Gate | Status | Evidence |
|---|---|---|---|
| 0 | preflight + validate_config + unit tests + offline dry-runs clean | ✅ 2026-07-28 | PR #1 |
| 1 | data-prep harness invoke-verified (skills listed, SageMaker API called, S3 written) | ✅ 2026-07-28 | deploy/evidence/VERIFICATION_phase1.md |
| 2 | curated.jsonl + stats in S3 (DeepSeek-R1 teacher via Bedrock) | ✅ 2026-07-29 | VERIFICATION_phase2_{pilot,main}.md |
| 3 | training complete via launch-and-release; EventBridge wake verified | ✅ 2026-07-29 | VERIFICATION_phase3.md |
| 4 | endpoint InService + smoke; quality gates evaluated (FAILED honestly — gate held) | ✅ 2026-07-29 | VERIFICATION_phase4.md |
| 5 | hands-off e2e: 5 iterations, final run 7 states to honest terminal state | ✅ 2026-07-29 | VERIFICATION_phase5.md |
| 6 | console live; auto model failover; OIDC trigger; bilingual docs; CI green | ✅ 2026-07-29 | VERIFICATION_phase6.md |

## Deployed AWS resources (us-east-1)

| Resource | Name | Created by | Notes |
|---|---|---|---|
| IAM roles ×8 | llmops-harness-execution, llmops-sagemaker-execution, llmops-{driver,start,resume,webhook}-lambda, llmops-sfn-execution, llmops-eval-execution, llmops-scheduler-invoke | deploy/01_iam.py + phase scripts | least-privilege, scoped to `llmops-*` |
| S3 bucket | (name in SSM `/llmops/storage/bucket`) | deploy/03_storage.py | versioned, SSE-S3, PAB, runs/ 90d lifecycle |
| DynamoDB ×4 | llmops-pipeline-runs (GSI job_name-index), llmops-stage-events, llmops-cost-estimates, llmops-cost-actuals | deploy/03_storage.py | PITR on; cost tables added v1.1.0 |
| EventBridge | bus `llmops-pipeline` + rule `llmops-sagemaker-job-state` (default bus) + rule `llmops-escalation-triage` (custom bus) | 03 + 07 | wake chain for launch-and-release; triage rule routes `EscalatedToHuman` → driver as `task=triage` (the custom bus carried ZERO rules until v1.1.x) |
| SNS topic | llmops-escalations | deploy/03_storage.py | EscalatedToHuman notifications; 1 **confirmed** email subscriber since 2026-08-02 (`PendingConfirmation: false`, verified via get_subscription_attributes) — was ZERO, so every `escalate_human` published into the void. Reaching a human took two steps: the deploy creates the subscription, the recipient clicks the link. `ensure_topic` reports pending separately for that reason, since a subscription that exists but is unconfirmed is the same silence in a new place |
| Macie | classification job `llmops-customer-data-pii` | deploy/03_storage.py --enable-pii-scan | SCHEDULED daily, 100% sampling, scoped `OBJECT_KEY STARTS_WITH customer-data/`; per-GB billed. The runtime role got read-only `macie2` the same day — a scan whose findings the auditing agent cannot read changes nothing a customer can see |
| Harnesses ×7 | llmops_{data_prep,finetune,eval,deploy,monitor,orchestrator,finops} | deploy/05_harnesses.py | full ids in SSM `/llmops/harness/*`; shared Memory attached; obs + online evals wired; all 7 on `global.anthropic.claude-fable-5` as deployed (GetHarness, 2026-08-02); driver auto-failover covers quota bursts |
| AgentCore Memory | llmops_shared_memory (SEMANTIC + EPISODIC) | deploy/04_wire_memory.py | shared across all 7 harnesses |
| Lambdas ×6 | llmops-{harness-driver,start-pipeline,resume-pipeline,webhook,finops-reconcile,monitor-sweep} | deploy/07_lambdas.py | driver: turn-continuation + auto model failover; monitor-sweep: own least-privilege role |
| Step Functions | llmops-pipeline (STANDARD) | deploy/07_lambdas.py | 25 states; 11 harness tasks on the happy path incl. EvalGenerate and MonitorHealth/MonitorReport, plus the remediation loop |
| Triggers | scheduler `llmops-nightly` (DISABLED) · `llmops-finops-daily` (ENABLED, 09:00 UTC) · `llmops-monitor-sweep-daily` (ENABLED, 08:00 UTC) · HTTP API `llmops-triggers` (/webhook, /runs) · secret `llmops/webhook` | deploy/08_triggers.py | endpoint in SSM `/llmops/triggers/api_endpoint`; both daily schedules use FlexibleTimeWindow OFF (each derives its period/id from the current date) |
| Admin console | `agent-cicd-admin` stack (Lambda+APIGW+DDB+Cognito) | ops-console deploy.sh | login secret `agent-admin/dashboard-login`; monitors orchestrator + finetune |
| Online evals | one config per harness (Correctness/GoalSuccessRate/ToolSelectionAccuracy, 100% sampling) | deploy/06_observability.py --evals | role llmops-eval-execution |

_Not deployed: VPC + endpoints (deploy/02_network.py). Not built at all: VPC-mode
harness variants — but the S3 skill mirror they depend on now exists and is what every
harness reads: all 19 skill sources across the 7 harnesses are `s3`, none are `git`
(guarded by tests/test_docs_claims.py). Earlier revisions named `harness.prod.json` files
that have never existed._

## Standing cost posture

Zero standing billable resources CREATED BY THIS PROJECT: no llmops-* SageMaker
endpoint, pipeline scheduler DISABLED, serverless spine idles free. Total v1 build cost
≈ $12–15. That scoping is load-bearing and used to be missing: the account also carries
`jumpstart-dft-hf-asr-whisper-large-v2`, InService since 2024-04-11 with 0 invocations
over 30 days, which Cost Explorer bills at **$36.36/day** (ml.g5.2xlarge ×1 —
confirmed by describe_endpoint_config, not the JumpStart-default guess the first run of
the schedule below had to make). It was found by that schedule, which scans the whole
account precisely because one restricted to `llmops-*` cannot find an unclaimed resource.
It is not ours to delete; reported, and awaiting the account owner. A PII scan
(`llmops-customer-data-pii`, Macie, SCHEDULED daily over `customer-data/`) went live
2026-08-02 and bills per GB — at 0.87 MiB that is under $0.01/scan.

Two recurring costs, both agent-turn cost only — every AWS API either of them calls is $0:

- the daily **09:00 UTC finops reconcile** at **~$1.5–4.5/month** (~$0.05–0.15 per invocation),
  reading read-only billing APIs;
- the daily **08:00 UTC monitor sweep** at **~$1–3/month**, reading `sagemaker:ListEndpoints`,
  `ListTags` and CloudWatch metrics.

These are the only two schedules enabled by default (`llmops-nightly` stays DISABLED because it
spends GPU money), for the same reason in both cases: a control that only runs when someone
remembers to run it is not a control. The sweep runs an hour ahead of the reconcile so what is
still standing is reported before the auditor tallies what was spent. Each writes one row per
invocation — `sweep#…` in `llmops-stage-events`, `#audit#` in `llmops-cost-actuals` — so a
schedule that silently *stopped* is visible; a cost control nobody can tell has stopped is not a
control either.

Guardrails, in order of who acts first: the console's **$2000 dual gate** (single-run worst
case, or project-to-date + this estimate), then the pre-existing account-level AWS Budget
`bedrock-monthly-dev` at **$1000/month**. See docs/COST.md.

## Harness versions & endpoint pins

Harnesses run DEFAULT endpoints (latest version). No student-model endpoint is
live (torn down after eval per cost rule).

## Model lineage

| Run | Teacher | Student | Result |
|---|---|---|---|
| run-phase2-main-0001 | us.deepseek.r1-v1:0 | Qwen3-1.7B QLoRA (job -r5, loss 0.50/0.52) | gates FAILED honestly (0/16; 6-trace floor); artifacts in S3 `runs/run-phase2-main-0001/training/` |
| mini e2e runs ×2 | same | 2-sample smoke-scale | FAIL_CLOSED_NO_INPUT → honest escalation (by design) |

## Known issues / open items

- Fable 5 vendor-quota 5xx bursts: this file claimed "all harnesses on Opus 5" until
  2026-08-02, when GetHarness on all 8 returned `global.anthropic.claude-fable-5`. The
  hot-swap was evidently never applied, or was reverted by a later deploy from config
  that still names Fable 5 — every `agents/*/harness.json` does. The mitigation that IS
  live is the driver's auto-failover; the model line is not. Asserted by
  tests/test_docs_claims.py rather than restated.
- GitHub Actions trigger requires one-time OIDC role setup + `AWS_OIDC_ROLE_ARN`
  repo secret (docs/TRIGGERS.md).
- v2 experiments queued: code-as-reasoning + augmentation; Kimi K3 teacher A/B;
  skill-feedback PRs to MLOps-agent-skills and agent-skills-best-practice.
