# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/); versioning: SemVer.

## [1.0.0] — 2026-07-29

### v1 complete — all six phases live-verified

- 6th agent: `llmops_orchestrator` (conductor) — NL goal → costed run plan,
  first-line escalation triage, cross-run reports.
- Orchestration hardening: harnessArn resolution (SSM), between-turn Lambda
  continuation (900s/840s), fail-closed quality gates, two-stage re-ask,
  automatic model failover on vendor 5xx bursts.
- Triggers: EventBridge Scheduler (disabled nightly), HMAC webhook + Admin API
  (HTTP API), GitHub Actions OIDC workflow.
- Admin console deployed (Cognito/APIGW/Lambda) and wired to the platform.
- Online evaluation configs on all harnesses (real API shape, live-introspected).
- Bilingual docs suite: ARCHITECTURE / TRIGGERS / TEST_RESULTS / CASE_STUDY
  (EN + zh-TW) + INFRASTRUCTURE; six per-phase evidence files.
- Live distillation run: 24-task ARC-AGI-2 dataset via DeepSeek-R1 ($5.60),
  QLoRA training after a 6-iteration self-remediation gauntlet, endpoint
  deployed after 5 versions/4 root causes, quality gates FAILED honestly.
- Total build cost ≈ $12–15 (budget $45–60).

## [0.1.0] — 2026-07-28

### Added — Phase 0 scaffold
- Five AgentCore Harness configs (`agents/*/harness.json`), all offline-validated:
  data-prep, finetune, eval, deploy, monitor — each mounting its LLMOps skills
  from [MLOps-agent-skills](https://github.com/timwukp/MLOps-agent-skills) and
  exposing the inline-function contract (`stage_complete`, `checkpoint`,
  `escalate_human`, + `job_launched` on finetune).
- Orchestration spine: Step Functions state machine + harness-driver /
  start / resume / webhook Lambdas (`orchestration/`).
- Least-privilege IAM (`deploy/iam/`), idempotent provisioning scripts
  (`deploy/01_iam.py`, `02_network.py` VPC + endpoints, `03_storage.py`).
- Contracts: run manifest schema, event vocabulary, ops-console report writer
  (`pipeline/contracts/`).
- Security: redaction pre-commit hook + CI check, SECURITY.md, AGENTS.md
  gotchas bank, bilingual-doc pairing enforcement.
- CI: compile, offline harness validation, policy JSON checks, offline
  dry-runs, unit tests, SVG geometry check.
