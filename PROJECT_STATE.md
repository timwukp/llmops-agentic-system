# PROJECT_STATE.md — deployed resources & progress (redacted)

Persistent memory for humans and agents. Update whenever AWS resources are
created/deleted or a phase gate passes. Never record account IDs or full ARNs.

## Current phase

**Phase 0 — scaffold** (gates passed 2026-07-28; foundation resources live).

## Phase gates

| Phase | Gate | Status | Evidence |
|---|---|---|---|
| 0 | preflight + validate_config + unit tests + offline dry-runs clean | ✅ 2026-07-28 | 5×`RESULT: OK`, 29/29 unit tests, SVG geometry CLEAN, redaction scan CLEAN |
| 1 | data-prep harness invoke-verified (skills listed, SageMaker API called, S3 written) | pending | — |
| 2 | curated.jsonl + stats in S3 (DeepSeek-R1 teacher via Bedrock) | pending | — |
| 3 | ModelTrained event; SFN launch→wait→resume trace | pending | — |
| 4 | evaluation/report.json gates PASSED; smoke_test.json | pending | — |
| 5 | hands-off e2e run: EventBridge → PipelineCompleted | pending | — |
| 6 | console live; rollback drill; CI green; bilingual docs | pending | — |

## Deployed AWS resources (us-east-1)

| Resource | Name | Created by | Notes |
|---|---|---|---|
| IAM roles ×6 | llmops-harness-execution, llmops-sagemaker-execution, llmops-{driver,start,resume,webhook}-lambda | deploy/01_iam.py | least-privilege, scoped to `llmops-*` |
| S3 bucket | (name in SSM `/llmops/storage/bucket`) | deploy/03_storage.py | versioned, SSE-S3, PAB, runs/ 90d lifecycle |
| DynamoDB ×2 | llmops-pipeline-runs (GSI job_name-index), llmops-stage-events | deploy/03_storage.py | PITR on |
| EventBridge bus | llmops-pipeline | deploy/03_storage.py | custom event vocabulary in pipeline/contracts/events.py |
| SNS topic | llmops-escalations | deploy/03_storage.py | EscalatedToHuman notifications |
| SSM params | /llmops/iam/*, /llmops/storage/* | deploy scripts | discovery layer; no ARNs in repo |

_Not yet deployed: VPC/endpoints (02, prod-only), shared Memory (04), harnesses (05),
observability deliveries + online evals (06), Lambdas, state machine, console stack._

## Harness versions & endpoint pins

_None yet._

## Model lineage

| Run | Teacher | Student | Adapter URI | Endpoint | Gate result |
|---|---|---|---|---|---|

## Known issues / open items

- llm-distillation skill must merge to MLOps-agent-skills main before Phase 2
  harness mounting (git skill source reads default branch only).
