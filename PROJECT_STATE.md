# PROJECT_STATE.md — deployed resources & progress (redacted)

Persistent memory for humans and agents. Update whenever AWS resources are
created/deleted or a phase gate passes. Never record account IDs or full ARNs.

## Current phase

**v1 COMPLETE** (all six phases passed 2026-07-28 → 2026-07-29). Next:
v2 experiments — code-as-reasoning distillation + augmentation; Kimi K3
teacher A/B (see docs/CASE_STUDY.md).

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
| DynamoDB ×2 | llmops-pipeline-runs (GSI job_name-index), llmops-stage-events | deploy/03_storage.py | PITR on |
| EventBridge | bus `llmops-pipeline` + rule `llmops-sagemaker-job-state` (default bus) | 03 + 07 | wake chain for launch-and-release |
| SNS topic | llmops-escalations | deploy/03_storage.py | EscalatedToHuman notifications |
| Harnesses ×6 | llmops_{data_prep,finetune,eval,deploy,monitor,orchestrator} | deploy/05_harnesses.py | full ids in SSM `/llmops/harness/*`; shared Memory attached; obs + online evals wired; currently on Opus 5 (Fable 5 fallback policy, AGENTS.md) |
| AgentCore Memory | llmops_shared_memory (SEMANTIC + EPISODIC) | deploy/04_wire_memory.py | shared across all 6 harnesses |
| Lambdas ×4 | llmops-{harness-driver,start-pipeline,resume-pipeline,webhook} | deploy/07_lambdas.py | driver: turn-continuation + auto model failover |
| Step Functions | llmops-pipeline (STANDARD) | deploy/07_lambdas.py | 9 states incl. remediation loop |
| Triggers | scheduler `llmops-nightly` (DISABLED) · HTTP API `llmops-triggers` (/webhook, /runs) · secret `llmops/webhook` | deploy/08_triggers.py | endpoint in SSM `/llmops/triggers/api_endpoint` |
| Admin console | `agent-cicd-admin` stack (Lambda+APIGW+DDB+Cognito) | ops-console deploy.sh | login secret `agent-admin/dashboard-login`; monitors orchestrator + finetune |
| Online evals | one config per harness (Correctness/GoalSuccessRate/ToolSelectionAccuracy, 100% sampling) | deploy/06_observability.py --evals | role llmops-eval-execution |

_Prod-only, not deployed: VPC + endpoints (deploy/02_network.py), harness.prod.json variants (S3-mirrored skills)._

## Standing cost posture

Zero standing billable resources: no SageMaker endpoints, scheduler DISABLED,
serverless spine idles free. Total v1 build cost ≈ $12–15.

## Harness versions & endpoint pins

Harnesses run DEFAULT endpoints (latest version). No student-model endpoint is
live (torn down after eval per cost rule).

## Model lineage

| Run | Teacher | Student | Result |
|---|---|---|---|
| run-phase2-main-0001 | us.deepseek.r1-v1:0 | Qwen3-1.7B QLoRA (job -r5, loss 0.50/0.52) | gates FAILED honestly (0/16; 6-trace floor); artifacts in S3 `runs/run-phase2-main-0001/training/` |
| mini e2e runs ×2 | same | 2-sample smoke-scale | FAIL_CLOSED_NO_INPUT → honest escalation (by design) |

## Known issues / open items

- Fable 5 vendor-quota 5xx bursts → all harnesses on Opus 5; switch back when
  stable (UpdateHarness hot-swap, ~15s each; driver auto-failover now handles
  future bursts).
- GitHub Actions trigger requires one-time OIDC role setup + `AWS_OIDC_ROLE_ARN`
  repo secret (docs/TRIGGERS.md).
- v2 experiments queued: code-as-reasoning + augmentation; Kimi K3 teacher A/B;
  skill-feedback PRs to MLOps-agent-skills and agent-skills-best-practice.
