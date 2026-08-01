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
  **All 19 skill sources across the 7 harnesses are `s3` today; none are `git`.** The S3
  mirror was the fix, for two reasons: VPC-mode harnesses can't reach GitHub at
  all, and main-branch drift otherwise silently changes agent behavior. The mirror and
  its IAM come from `ensure_skills` in `deploy/03_storage.py`; the sources themselves now
  point at `s3://<DATA_BUCKET>/skills/...`, resolved at deploy time by
  `deploy/config_subst.py` (the bucket name embeds the account id, which may not appear in
  a file of this public repo). `tests/test_docs_claims.py` derives the counts and the KIND
  from the configs, so this line cannot go stale silently, and it rejects a *mixed* state:
  a partial migration means some harnesses read a pinned snapshot while others still float
  on the skill repo's main, which is the drift the migration exists to stop.
- **An unresolved `<TOKEN>` in a config is a deploy-time FAILURE, not a warning.**
  `config_subst.resolve()` raises on any `<UPPER_CASE>` left after substitution — a typo
  like `<DATABUCKET>` passes the linter, passes `validate_config.py`, and is accepted by
  the API. `05_harnesses.py` / `update_harness.py` / `create_harness.py` all resolve before
  the first API call, and the live bucket comes from SSM `/llmops/storage/bucket` in
  preference to the derived name (a deploy that passed `01_iam.py --bucket` has a different
  one).
- **A bad skill source fails at SESSION START, not at `UpdateHarness`** — a wrong path
  or a `SKILL.md` missing its YAML frontmatter is accepted by the control plane and then
  fails every invocation. So switching sources requires the objects to be in place
  first; the switch is not reversible by config alone once sessions start failing. READY is
  therefore no evidence at all: the proof that the s3 sources work is a live session that
  ran and wrote its artifacts.
- **The mirror is at `s3://<bucket>/skills/` and all 19 sources are now `s3` and point
  at it.**
  `deploy/03_storage.py --skills-src <checkout>` derives what to mirror from
  `agents/*/harness.json`, validates every `SKILL.md`'s frontmatter *before* uploading,
  and reads each one back. 66 files / 11 distinct skills behind the 19 mounts. It derives
  the list from **both** source kinds: collecting only `git` mounts inverted at exactly
  the switch — after it, S3 is the only copy any agent reads, so that is precisely when
  the mirror must keep syncing, and the coverage guard would have compared 0 mounts
  against 0 git sources and passed.
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
- **A Lambda that is an EventBridge target cannot be deployed from a branch that does
  not know the rule exists.** The driver shipped without `triage_event_from_bus` while
  `llmops-escalation-triage` was ENABLED and targeting it; every escalation then arrived
  as a raw envelope and died on `KeyError: 'run_id'`. Every offline guard stayed green,
  because they compare `EVENTS_NEEDING_A_RULE` against the rules *this tree's* deployer
  builds — a branch missing the declaration AND the rule AND the translator is
  self-consistent. Only the bus knows what is live, so `07_lambdas.py` asks it at deploy
  time (`live_bus_translator_gap`) and raises rather than warns. `BUS_DELIVERY_TRANSLATORS`
  in `pipeline/contracts/events.py` declares which function reads which detail-type; an
  `InputTransformer` on the rule's target is the accepted alternative.
- **`update_item` is an UPSERT, so "update a row" and "create a row" are one call.** On a
  key with no row DynamoDB creates one from the key plus whatever `SET` writes. The driver's
  escalate path therefore *minted* a run for every escalation by something that is not a run
  — live, `sweep-2026-08-01` sat in `llmops-pipeline-runs` as `{run_id, status: escalated}`
  with none of the attributes a real run carries. Gate creation with
  `ConditionExpression="attribute_exists(<pk>)"` rather than with a hand-maintained list of
  callers-that-are-not-runs; the earlier list (`stage == "finops"`) is exactly why the sweep,
  added later, reintroduced it. Absorb **only** `ConditionalCheckFailedException`, matched by
  botocore error *code* — absorbing everything would leave a run that really escalated at
  `running`. And note the co-defect: `FakeTable.update_item` in `tests/test_orchestration.py`
  used to drop writes to an absent key, which made the whole class untestable. A double more
  forgiving than production hides exactly the bugs production will have. One more thing the
  row write was hiding: it was the *only* durable record an escalation left, because
  `handle_escalate` wrote no stage event at all — so check what a conditional write was
  standing in for before you make it conditional. The escalation is now recorded in
  `llmops-stage-events` on both paths, wrapped, so a failed record cannot withhold the alert.
- **A checklist guard that carries its own copy of the checklist cannot detect drift.**
  The console's Data-readiness panel is supposed to ask every question the orchestrator's
  consult protocol tells the agent to answer. Its guard hand-copied seven paths and
  asserted the console contained them — so the test agreed with the console and with
  itself, while the prompt's `data` block specified **nine**. `datasheet.provenance` and
  `readiness_report_uri` were missing from the panel with every test green, and the second
  is the pointer to the Data Readiness Report — where the audit's **PII scan** lands. The
  panel showed "PII disposition" as answered from a claim in the plan while omitting the
  only link to the artifact that examined the data. The guard now parses the key list out
  of `agents/orchestrator/harness.json`, and a second guard asserts that derivation is
  still a derivation. Same rule as the documented-test-count guard: derive from the real
  source, and if the source is a model prompt, parse the prompt.
- **A Macie session being ENABLED is not coverage, and a job list is not coverage.** Live,
  this account's session was `ENABLED` and `list_classification_jobs` returned a COMPLETE job
  named `scan` — which was `ONE_TIME`, created **2021-02-23**, named 25 unrelated buckets and
  processed **0 objects**. Nothing looked at `customer-data/`. Only the bucket list plus the
  scoping answers the question, which is why `macie_job_covers()` in `deploy/03_storage.py` is
  a pure function with its own tests: a job can name our bucket and still include only
  `runs/`, and a `bucketCriteria` job is **undecidable** from its definition (reported
  separately, never counted as coverage). `ensure_pii_scan` always REPORTS the gap and creates
  the job only under `--enable-pii-scan`, because a `SCHEDULED` job is recurring per-GB work
  and a deploy that starts billing silently is the same class of surprise as a silent security
  downgrade. Two live API constraints, neither in the docs: `UpdateClassificationJob` accepts
  only `(jobId, jobStatus)`, so a job's scope is **immutable** and a wrongly-scoped job must be
  cancelled and replaced (the step says so rather than claiming an update); and
  `CreateClassificationJob` takes a `clientToken`, so a repeat with a fresh token creates a
  **second** job — idempotency has to come from finding our own job by name.
  And the grant that makes any of it visible: `simulate_principal_policy` on
  `llmops-harness-execution` returned **implicitDeny** for every `macie2` read, so a scan
  would have run, billed, and been unreadable by the `audit` task that writes the Data
  Readiness Report the console links. `MacieFindingsReadForDataAudit` is read-only in both
  directions — no `CreateClassificationJob` (billable work is the deploy's call) and no
  `UpdateMacieSession` (an agent must not be able to switch off the check it is judged by).
  The audit's own scan stays heuristic regex and still says so; the prompt now also requires
  it to write *"no Macie classification job covers this data"* when nothing does.
- **Notify on independent channels, and never let the weakest one gate the rest.** The
  driver's escalate path publishes to SNS, writes a stage event, emits `EscalatedToHuman`,
  and settles the task token. The SNS publish was first and unwrapped, so one failed publish
  cost all four — including the settle, which parks a live token until the stage timeout.
  And **`llmops-escalations` has zero subscribers**, so that was the channel with a
  known-zero audience gating the ones that work (`ensure_topic` in `deploy/03_storage.py`
  says so out loud instead of reporting the topic healthy; fix with `--escalation-email`).
  Wrap each notification separately and order them so the state-releasing call is last.
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
