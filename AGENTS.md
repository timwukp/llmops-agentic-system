# AGENTS.md — orientation for AI agents working on this repo

This file front-loads what an AI coding agent needs to work here safely and correctly.
(Convention borrowed from AWS's own repos and timwukp/Harness-agentic-AI-agent-best-practices-and-use-case.)

## What this repo is

An end-to-end **LLMOps platform run autonomously by Bedrock AgentCore Harnesses**.
Flagship pipeline: knowledge distillation — teacher **DeepSeek-R1 (Bedrock, serverless)**
generates training data → student **Qwen3-1.7B** is QLoRA-fine-tuned as a SageMaker
training job → evaluated against quality gates → deployed to a SageMaker endpoint →
monitored. Orchestration spine: Step Functions Standard + a thin harness-driver Lambda.
Five worker harnesses (`llmops_data_prep`, `llmops_finetune`, `llmops_eval`,
`llmops_deploy`, `llmops_monitor`), each mounting its domain skills from
[MLOps-agent-skills](https://github.com/timwukp/MLOps-agent-skills).

Built WITH two agent skills — keep using them when you modify this repo:
1. **agentcore-harness-builder** (timwukp/agent-skills-best-practice) — the prescribed
   workflow for anything touching harness configs, memory, observability, versioning.
2. **MLOps-agent-skills LLMOps chain** — the domain methodology mounted into each harness.

## SDK gates (hard requirements)

| Dependency | Minimum | Why |
|---|---|---|
| boto3 / botocore | **1.43.51** | Harness APIs don't exist below this |
| AWS CLI | v2 ≥ 2.34.57 | same |
| Python | 3.12 | Lambdas + scripts |

Always run `deploy/preflight.py --region <region>` before any AWS work — it gates
versions and introspects the live CreateHarness/UpdateHarness schemas.

## Gotchas bank (live-verified — do not re-derive)

- **Dual ARNs**: every harness has `harness/...` AND an auto-created `runtime/harness_<name>...`.
  Callers need BOTH `bedrock-agentcore:InvokeHarness` and `InvokeAgentRuntime`.
  `InvokeAgentRuntimeCommand` runs shell in the session VM and is NOT gated by
  `allowedTools` — gate it with IAM.
- **Managed memory IAM**: managed memory auto-creates a Memory named `harness_<name>_*`;
  the execution role needs event/retrieval permissions on `arn:...:memory/harness_*`
  or the first InvokeHarness fails `AccessDeniedException ... ListEvents`.
- **UpdateHarness**: only `memory` / `environmentArtifact` / `authorizerConfiguration`
  wrap in `optionalValue`; `model`/`environment`/`truncation` pass directly;
  `tags` go via `TagResource`; `clientToken` ≥ 33 chars.
- **Response shapes**: Create/GetHarness nest under `harness` with ARN field `arn`
  (not `harnessArn`); InvokeHarness returns `stream`; `runtimeSessionId` ≥ 33 chars.
- **allowedTools**: plain names only. `browser_*` globs match nothing and silently
  hide the tool.
- **Git skill source reads the DEFAULT branch only** — a skill change must merge to
  the skill repo's main before a fresh session picks it up. Production harnesses use
  the S3-mirrored skill snapshot instead (see `deploy/05_mirror_skills.py`), because
  (a) VPC-mode harnesses can't reach GitHub and (b) main-branch drift must not
  silently change production agent behavior.
- **Observability**: `OTEL_TRACES_SAMPLER=always_on` env var is mandatory or
  evaluations/insights sit at zero forever. X-Ray delivery takes no `outputFormat`.
- **Turn budget**: harness `timeoutSeconds` is 840 here (driver Lambda caps at 900s).
  Long work checkpoints via the `checkpoint` inline function; training jobs are
  launch-and-release (`job_launched` → Step Functions `waitForTaskToken` →
  EventBridge SageMaker state-change rule → resume in a FRESH session).
  State lives in the S3 manifest + DynamoDB, never in the session.
- **DeepSeek-R1 output contains `<think>...</think>`** — the data-prep agent strips
  it by default (sequence-level KD keeps final answers only).

## Security rules (non-negotiable)

- This is a PUBLIC repo. **No AWS account IDs, no account-bearing ARNs, no bucket
  names with account IDs, no credentials** — anywhere, including docs, evidence
  files, and commit diffs. Use `<ACCOUNT_ID>` placeholders; deploy scripts substitute
  at run time. `hooks/pre-commit` and `.github/workflows/redaction-check.yml` enforce.
- Least-privilege IAM only. No `*FullAccess` managed policies.
- Production harnesses run VPC-mode with interface endpoints (see `deploy/02_network.py`);
  dev configs (`agents/*/harness.json`) are PUBLIC-network for iteration speed —
  prod variants are `agents/*/harness.prod.json`.

## Repo conventions

- Every deploy/wiring script is idempotent, supports `--dry-run`, and publishes its
  outputs to SSM under `/llmops/*`.
- Every phase of work ends with a REAL AWS verification, logged in
  `deploy/evidence/VERIFICATION_*.md` and summarized in `docs/TEST_RESULTS.md`.
  "Always invoke before declaring success."
- Docs are bilingual: `X.md` (EN) + `X.zh-TW.md` (繁體中文), updated in the same PR.
- Architecture SVGs are GENERATED (`docs/gen_architecture_svg.py`) and layout-checked
  (`tests/check_svg_geometry.py` — no wire crossings, no wire through a card).
  Never hand-edit the SVGs.
- `PROJECT_STATE.md` records deployed resource names/versions (redacted); update it
  whenever you create or delete AWS resources.
