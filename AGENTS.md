# AGENTS.md

## Model failover is a first-class design layer (not an emergency measure)

Live-established 2026-07-29: vendor model quotas are a HARD constraint — even
AWS-internal accounts are rate-limited by the model provider; quota increases
are not a plan. A multi-agent platform is its own token-flood generator (6
harnesses × agent loops × long streams), so 5xx bursts against the premium
model tier are structural, not incidental.

Design rules:
1. Every harness has a **fallback model chain**: `global.anthropic.claude-fable-5`
   → `global.anthropic.claude-opus-5` (same-family, zero prompt changes).
   Hot-swap via `UpdateHarness` (~15s to READY); sessions survive the swap.
2. Failure signature that means "switch": repeated
   `runtimeClientError → InternalServerException/ServiceUnavailableException`
   from ConverseStream while a direct single-shot probe of the same model
   succeeds — that's quota pressure, not an outage (it never surfaces as an
   explicit ThrottlingException).
3. **Mixed allocation** spreads quota pressure by design: premium tier (Fable 5)
   for judgment-heavy agents (orchestrator, eval), Opus 5 for process-execution
   agents (data-prep, deploy, monitor).
4. Phase-6 hardening: the harness driver auto-swaps after N consecutive 5xx
   and emits a ModelFailover event (today's manual procedure, automated).
 — orientation for AI agents working on this repo

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
- **Git skill source reads the DEFAULT branch only** (there is no branch field) — a
  skill change must merge to the skill repo's main before a fresh session picks it up.
  **All 19 skill sources across the 7 harnesses are `git` today; none are `s3`.** An S3
  mirror is the fix, for two reasons: VPC-mode harnesses can't reach GitHub at
  all, and main-branch drift otherwise silently changes agent behavior. The mirror and
  its IAM now exist (`ensure_skills` in `deploy/03_storage.py`); the source *switch* is
  what has not happened. `deploy/05_mirror_skills.py` and `agents/*/harness.prod.json`
  were described here as existing files and have never existed in any branch.
  `tests/test_docs_claims.py` now derives the counts from the configs, so this line
  cannot go stale silently.
- **A bad skill source fails at SESSION START, not at `UpdateHarness`** — a wrong path
  or a `SKILL.md` missing its YAML frontmatter is accepted by the control plane and then
  fails every invocation. So switching sources requires the objects to be in place
  first; the switch is not reversible by config alone once sessions start failing.
- **The mirror is live at `s3://<bucket>/skills/` but no source points at it yet.**
  `deploy/03_storage.py --skills-src <checkout>` derives what to mirror from
  `agents/*/harness.json`, validates every `SKILL.md`'s frontmatter *before* uploading,
  and reads each one back. 66 files / 11 distinct skills behind the 19 mounts.
- **An s3 skill source is fetched by `llmops-harness-execution`, NOT by the deployer.**
  The mirror's own `head_object` read-back passes under admin credentials and proves
  nothing about the role that actually fetches at session start:
  `simulate_principal_policy` on that role returned **implicitDeny** for
  `skills/*` while the role file granted `skills-mirror/*` — a prefix that never held a
  single object. Verify a new prefix with `simulate_principal_policy`, not with your own
  `aws s3` call. The grant is `GetObject` + condition-scoped `ListBucket` only (a source
  resolves a whole tree, so Get without List strands it), and deliberately separate from
  `S3PipelineObjects`, which carries `PutObject`: an agent that can write its own skill
  tree can rewrite the instructions it is judged against next session.
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
- The VPC with interface endpoints is built by `deploy/02_network.py`. Harness configs
  (`agents/*/harness.json`) are PUBLIC-network for iteration speed. VPC-mode harness
  variants are **not built yet** and depend on the S3 skill mirror above (a VPC-mode
  harness cannot resolve a git skill source).

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
