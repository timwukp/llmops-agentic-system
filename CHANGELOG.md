# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/); versioning: SemVer.

## [1.1.0] — 2026-07-31

### FinOps — cost estimation, a $2000 approval gate, and a 7th runtime

Before this release the pipeline spent real money with no cost surface anywhere: nothing
estimated a run, nothing reconciled it, nothing could stop an expensive one. The gap was
concrete — the 2026-07-31 QLoRA run billed **$10.77** and that figure existed only because a
human ran `describe-training-job` and multiplied by a rate recalled from memory.

- **`pipeline/contracts/cost_model.py`** — the one place estimate arithmetic lives. Line-itemised
  estimates (never a bare total), each row carrying its `basis` formula and `rate_source`.
  Calibrated against the $10.77 run: **0.0% delta** on the training line (0.664 rows/s and 670 s
  setup are that run's own measurements, not guesses).
- **7th AgentCore runtime `llmops_finops`** (財務審計員／統計員／報告員) — daily 09:00 UTC
  reconcile, plus on-demand `pricing_refresh` and `report`. Read-only on billing: it reports and
  flags, and cannot stop a run. Sits beside `llmops_orchestrator` above the state machine, so it
  never appears in a run's stage sequence.
- **Console Cost tab** — estimate, approval queue, itemised actuals by project/service/run,
  estimate-vs-actual variance, and rate-card health.
- **The $2000 gate is dual**: approval fires when either this run's worst case exceeds the
  single-run limit, or project-to-date + this estimate exceeds the cumulative one. Twenty $150
  runs are the same exposure as one $3000 run, and each passes a single-run check alone.
- Gates on **`worst_case_usd`, not `total_usd`** — the remediation loop can re-run finetune up to
  `max_iterations`, so approving $2000 that can become $6000 is not a gate.
- **Separation of duties** — Cognito group `llmops-approver`, checked server-side on every call;
  self-approval is rejected with 403, not merely flagged. `rejected` and `launched` are terminal
  both ways, so a refusal cannot be quietly retried and one approval cannot launch two runs.
- **Every failure path fails closed** — no cost model → approval *required*; no rate card →
  estimate *refused* (503) rather than a $0-with-warnings total; group lookup failure → deny.
- Two new tables (`llmops-cost-estimates`, `llmops-cost-actuals`) and an S3 rate-card cache with
  dated history, so an old variance can be re-derived against the rates live at estimate time.
- **147 FinOps tests** (52 cost model + 36 agent/Lambda + 59 console), all without AWS
  credentials; 252 in the suite. Mutation-checked: breaking each guard was verified to fail a
  test, which found two guards a green suite did not cover.
- Bilingual [docs/COST.md](docs/COST.md) / [docs/COST.zh-TW.md](docs/COST.zh-TW.md).

### Verified facts that shaped the design (live, read-only, 2026-07-31)

- **The Price List API prices neither DeepSeek-R1 nor Fable 5** — the teacher and harness models,
  the two largest token consumers. Its `AmazonBedrock` model list tops out at Claude 3 Haiku /
  DeepSeek V3.2. So realized billing rates (cost ÷ quantity from our own invoice) outrank the
  published price list; Price List is the fallback for never-used resources.
- **Cost allocation tags are unusable today** — `project`/`Project` both Inactive, and a
  tag-filtered CE query returns **$0.00** for a day with real spend. Attribution is therefore by
  explicit resource match (`run_id` is already inside job and endpoint names), which needs no
  tagging at all. Tags are not retroactive, so the $10.77 run will never carry one.
- **Attribution must never be by service.** This account's month-to-date total was **$27,491**
  while this project's share was **~$10–15**; the rest includes unrelated SageMaker Canvas
  (~$296) and a JumpStart Whisper endpoint (~$18/day). A service filter would have reported
  thousands of dollars of someone else's spend as ours — and tripped the $2000 gate immediately.
- **Cost Explorer lags ~24 h** and marks recent periods `Estimated: true`, so reconciliation is
  async and re-runnable, and a run counts as settled only when *every* row for it is settled.

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
