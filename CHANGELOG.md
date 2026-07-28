# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/); versioning: SemVer.

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
