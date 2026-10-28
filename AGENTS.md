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
   Switch via `InvokeHarness`'s per-invocation `model` override, which is scoped
   to the one call. **Not** `UpdateHarness`: that mints a new harness version and
   repoints the DEFAULT endpoint, and every stage here invokes through DEFAULT
   (no `qualifier` anywhere in `state_machine.asl.json`) — so a swap during a
   storm moves the model under every other run in flight, then has to be undone
   by a restore running on the same control plane that is 5xxing.
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
- **A create that omits `memory` provisions MANAGED memory, so no create here omits it.**
  `memory` is a union (`agentCoreMemoryConfiguration | managedMemoryConfiguration |
  disabled`) and absence selects managed. All 7 harness configs declare `{"disabled": {}}`;
  `04_wire_memory.py` attaches the shared BYO memory afterwards, and `05_harnesses.py` never
  sends `memory` on update, so a redeploy does not undo it. The configs used to say "Created
  WITHOUT memory" while every create got a managed memory: audited 2026-10-13
  (`deploy/evidence/VERIFICATION_harness_memory_audit.md`), each of the 7 llmops harnesses had
  an orphaned managed memory created in the same second as the harness — **UpdateHarness does
  not delete the memory it replaces**. All 7 were empty (0 actors / 0 records) and were deleted
  2026-10-14 after a re-check. A harness re-pointed away from a managed memory later leaves the
  same residue; whether DELETING a harness removes its managed memory is not established. The
  daily `sweep` (`llmops_monitor`) now reports such memories as `memory_orphans`, reading
  metadata only: `GetHarness` is scoped to `harness/llmops_*`, and no memory contents. The name
  pattern has changed: `harness_<name>_<4hex>-<10>` for creates through 2026-08-04,
  `<harnessName>-<10>` from 2026-08-11 (and a 2026-10-13 probe, whose create called
  `CreateMemory` on `memory/<harnessName>-*` and reached READY only with `{"disabled": {}}`).
  So the execution role's `ManagedMemoryEvents` grant on `memory/harness_*` does NOT match a
  managed memory created today; an agent on one would hit the `AccessDeniedException ...
  ListEvents` that grant was added to fix (inferred from that earlier live failure, not
  re-measured). `create_time_memory()` in `deploy/create_harness.py` refuses a
  create with no `memory`; `05_harnesses.py` fails a create that comes back managed anyway;
  `tests/test_harness_memory.py` derives this paragraph's count and grant from the configs and
  the role file, and checks that every `create_harness(` call in the repo sends `memory`.
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
- **`additionalParams` is a raw ConverseStream pass-through, and both ways into it silently
  drop harness state.** InvokeHarness has no prompt-caching field: a `cachePoint` block in
  the top-level `messages` is rejected outright (`must be one of: text, toolUse, toolResult,
  reasoningContent`). But `bedrockModelConfig.additionalParams` forwards verbatim to
  ConverseStream — an unknown key returns its full member list (`modelId, messages, system,
  inferenceConfig, toolConfig, guardrailConfig, additionalModelRequestFields, promptVariables,
  additionalModelResponseFieldPaths, requestMetadata, performanceConfig, serviceTier,
  outputConfig`), so caching *is* reachable and was measured working
  (`cacheWriteInputTokens 3568` then `cacheReadInputTokens 3568`). Both routes are traps:
  * `additionalParams.system` **REPLACES** the harness `systemPrompt`. Asked a question only
    the prompt could answer, the same harness said `STARTER "Data Readiness Audit"` natively
    and `NO-PROTOCOL` with `system` set. Input tokens *fell* 10840 → 6644 and the agent still
    answered fluently, so every signal reads as an optimization.
  * `additionalParams.messages` **REPLACES** the message list including session history. A
    codeword stored one turn earlier came back `NONE` — while `cacheReadInputTokens=3568`
    made the call look like a pure win.
  * Echoing `GetHarness`'s `systemPrompt` back is not a fix either: the runtime **injects a
    skills manifest the control plane does not return** (a deterministic 1148 extra input
    tokens; 12707 native vs 11559 echoed, measured 3/3). Echoed back, the agent listed **2 of
    4** skills and lost the `.agents/skills/s3/<name>/SKILL.md` path it reads them by.
  Measured shape of a real consult turn: `wall=59.0s ttft=26.4s rounds=2 model_ms=52030 (88%
  of wall) in_tok=31691` — the ~11 KB system prompt is resent uncached on every round, and the
  round-trips ARE the turn. Caching it needs a first-class field on InvokeHarness; until then
  the lever is fewer round-trips, not a cheaper one.
- **A mount makes a skill readable; only the prompt makes the agent read it.** The
  orchestrator's prompt named `(llm-agent-orchestration, ml-solution-design)` as "your
  methodology — consult them before acting" while **four** were mounted; the two later mounts
  were never added to the sentence, and the missing `llm-data-preparation` is the skill for
  step 0 DATA DISCOVERY, the consult protocol's own opening move. The existing guard asserted
  the MOUNT, which was intact. Live, the agent listed all four (the runtime manifest above),
  so it could see them and was still told in prose that two were its methodology — and prose
  is what says "consult them". The guard now derives the expected names from each harness's
  `skills` list in **both** directions, across every agent: a mount nobody names fails, and a
  name nothing mounts fails.
- **A read-modify-write "append-only" log is neither append-only nor safe, and a cap
  applied before the split truncates both copies.** `_transcript_append` in
  `deploy/console/lambda_function.py` had three defects in twelve lines, all of which the
  suite was blind to because the `wired` fixture stubs the function out (the `audited`
  fixture now puts the real one back):
  * It read the whole `transcript.jsonl`, concatenated, and put it back — with
    `except Exception: old = b""` on the read. Any transient failure (503, throttle) read
    as *"no file yet"*, so the put **replaced the entire history** with just the newest
    lines. The one artifact whose job is to survive was one error from erasure. Fixed by
    writing **one timestamped object per append** (`tasks/<id>/transcript/<iso>-<rand>.jsonl`),
    which removes the read entirely — nothing to fail into silence, nothing to overwrite,
    and two concurrent writers cannot lose each other (`close_task` is permitted while a
    turn is in flight, because `thinking` is not in `TASK_TERMINAL`, so that race is
    reachable — DynamoDB's `list_append` kept both messages while the S3 copy dropped one).
    Keys sort lexicographically into chronological order.
  * Every caller applied `[:8000]` **before** the DynamoDB/S3 split, so the "full-text
    audit copy" held a truncated copy of a truncated record. Live, one **assistant** reply
    sat at exactly 8000 characters in both — the kind of message an acceptance is signed
    against. The cap now lives inside `_append_messages` (`MSG_TEXT_MAX`, for the one 400 KB
    DynamoDB item the UI renders); the audit copy keeps the message whole under a separate,
    declared `TRANSCRIPT_TEXT_MAX`.
  * The audit write was **unwrapped and last** inside `_append_messages`, so one S3 failure
    propagated out and skipped everything after that call *in the caller* — for
    `accept_task` that is `_task_event(PlanAccepted)` **and** `_enqueue_task_turn(accept=True)`:
    a KMS-signed acceptance stranded at `accepting` with no worker, escapable only by the
    20-minute `STALE_TURN_MIN` hatch. Same shape as the SNS publish below. Use
    `_safe_transcript_append`.
  Nothing reads the transcript back today — it is a write-only audit artifact, which is
  exactly why nothing noticed. Verify a write-only artifact by reading it.
- **Notify on independent channels, and never let the weakest one gate the rest.** The
  driver's escalate path publishes to SNS, writes a stage event, emits `EscalatedToHuman`,
  and settles the task token. The SNS publish was first and unwrapped, so one failed publish
  cost all four — including the settle, which parks a live token until the stage timeout.
  And **`llmops-escalations` had zero subscribers** at the time, so that was the channel
  with a known-zero audience gating the ones that work (`ensure_topic` in
  `deploy/03_storage.py` says so out loud instead of reporting the topic healthy; fixed
  2026-08-02 with `--escalation-email` plus the recipient's own click, and the ordering fix
  stands on its own regardless
  — a channel that works today is still the wrong thing to gate a token settle on).
  Wrap each notification separately and order them so the state-releasing call is last.
- **An Athena workgroup that sets result encryption cannot write a CMK-encrypted S3 Table.**
  With `ResultConfiguration.EncryptionConfiguration` set, every `INSERT` into a table in
  `llmops-curated` failed `INTERNAL_ERROR_QUERY_ENGINE` (`Retryable: False`, empty message), and
  `SSE_KMS` naming the table's *own* key fails the same way. Under the same workgroup an `INSERT` into
  an SSE-S3 table succeeds, and so do `SELECT` and `UNLOAD` against the CMK table, so reads tell you
  nothing (measured 2026-10-15). `llmops-lakehouse` therefore sets none, and the data bucket's default
  encryption covers results and extracts. `10_lakehouse.py --verify` checks both halves, and
  `ensure_workgroup` removes an override it finds rather than reporting the workgroup `exists`.
- **The harness can answer an inline function `Unknown tool` itself, and the call is then not the
  turn's last block.** In the monitor runtime log, a `stage_complete` call often gets
  `tool not found in registry` with `available_tools=<['shell', 'skills']>`, and the model receives
  an error toolResult. What the agent does next decides whether the driver hears the call. If it
  ends the turn, the call is the last block and the end_turn override services it: 33 of the 56
  serviced sweep turns on record, and every sweep since 2026-09-22. If it runs shell first
  (uploading a `stage_complete.json`, echoing `STAGE_COMPLETE`), or types the call, the driver sees
  only `tool=shell`. Sweeps 2026-08-18, -08-31, -10-06 and -10-07 died that way with their reports
  in S3, and their `PipelineFailed` matched no rule. So read every serviced block in the stream, not
  the last one (`serviced_tool_use` in `_drain`). Never gate typed-call recovery on "no block at
  all". Answer such a call as text, since its id has already been answered. Why the registry omits
  the inline functions is not established, and it is not confined to the scheduled jobs: the
  August pipeline rows show `recovered_ending_in_prose = 0`, but every stage of the 2026-10-15
  lakehouse run `run-20261015T065307Z-97655783` was serviced through it (EvalGate 13 times). Treat
  the end_turn path as the normal one, not a fallback. And bound what it can re-buy: a serviced call re-arms the
  prose budget, so a call refused every turn needs its own cap (`REJECTED_CALL_BUDGET`), or an agent
  that hears the refusal and re-sends anyway runs forever. `deploy/evidence/VERIFICATION_sweep_unheard_calls.md`.
- **An end_turn call has no pending handoff either, so ANY reply to it goes as text.** The
  "answer as text" rule above was applied to the shadowed call only. The end_turn override kept
  the `toolUseId` and replied with an echo plus `toolResult`, and the runtime refuses that:
  `runtimeClientError: Inline function callback has no pending handoff in this session`. On the
  success path `_ack_terminal` swallowed the refusal, so 33 serviced sweeps never showed it. A
  *rejection* is not swallowed. It becomes the next turn's input, dies as a stream error, and
  the salvage prompt ("call your pending inline function") never says why. Live,
  run-20261016T012010Z-9de45cbf's deploy claimed the prefix `deployment/model/merged/`, which
  `head_object` can only miss. The agent re-sent the identical claim ~25 times, then escalated,
  saying the tool was "unregistered". The override now marks the call `ended_turn`. The driver
  logs every rejected claim, and a rejection names a prefix as a prefix (the driver role has
  `HeadObject` and no `ListBucket`, so the agent has to name objects). The same run showed the
  other half: **Deploy's Catch went straight to `EscalateFail` and stranded an InService
  endpoint**. It now goes through `TeardownAfterDeployFailed`. Both are live since 2026-10-21
  (#176, `ecc2e50`; the driver is now at #177's `7f3d0ee`, which keeps it), and neither path has run live yet.
  `deploy/evidence/VERIFICATION_deploy_settle_and_teardown.md`.
- **Observability**: `OTEL_TRACES_SAMPLER=always_on` env var is mandatory or
  evaluations/insights sit at zero forever. X-Ray delivery takes no `outputFormat`.
- **Turn budget**: harness `timeoutSeconds` is 840 here (driver Lambda caps at 900s).
  Long work checkpoints via the `checkpoint` inline function; training jobs are
  launch-and-release (`job_launched` → Step Functions `waitForTaskToken` →
  EventBridge SageMaker state-change rule → resume in a FRESH session).
  State lives in the S3 manifest + DynamoDB, never in the session.
- **One handoff per job the pipeline waits on, and the driver checks which job.** Finetune's
  `launch` used to run a 20-step probe, read its s/step, launch the full job, *and* "call
  job_launched and END YOUR TURN". That is one handoff for two jobs. On 2026-10-15 the agent named
  the probe three runs out of three. The machine resumed into `FinetuneAnalyze` on the probe, and
  the full job was launched from analyze, which nothing resumes; one run waited there through a
  capacity queue until the stage timed out. Raising analyze's timeout (PR-11's stopgap) only
  lengthens that wait.

  Now `launch`/`remediate` hand off the probe and `FinetuneLaunchFull` (task `launch_full`) hands
  off the full job; analyze waits on nothing. `JOB_HANDOFFS` in `pipeline/contracts/tasks.py` says
  which task hands off which kind. The driver reads the kind off the job's own `max_steps`
  (DescribeTrainingJob) and rejects a wrong-kind, nonexistent or no-handoff `job_launched` back
  into the turn. The prompt said the right thing every time, so the check is on the job, not the
  words. A new launch task needs an entry there, and the test derives the key set from the prompt
  bullets both ways.
- **Registering S3 Tables with Lake Formation revokes the pipeline's IAM access; there is no
  "keep IAM" mode.** AWS's migration page (`change-access-iam-to-lf.html`) says the register step
  "will revoke all existing IAM-based access", and says to keep hybrid access false. Every pipeline
  principal reads the tables through IAM, so Unified Studio is IAM-governed instead: an IAM-based
  domain reads through its execution role (`deploy/11_studio.py`). That role must carry
  `SageMakerStudioAdminIAMDefaultExecutionPolicy`, which ALLOWS `glue:DeleteCatalog` on
  `s3tablescatalog`, `lakeformation:RegisterResource`/`PutDataLakeSettings` and `iam:PassRole` of any
  role (measured with `simulate_custom_policy`, v27), so the scoping is explicit Deny, and the tests
  require every refused case to be an explicit Deny of ours. An IAM-based domain is a console step:
  its `CreateDomain` carries `preferences.DOMAIN_MODE=EXPRESS` and `iamSignIns`, then
  `ProvisionDomain` and `StartAccountBootstrapAction`, none in the public SDK/CLI (CloudTrail,
  2026-10-24). It stores the execution role as `serviceRole` with `domainExecutionRole` null and an
  id `dzd-…`, so look it up by either field. Under IAM, a Bronze schema is very likely as hidden as its
  rows: S3 Tables reads the Iceberg metadata file with `GetTableData`, and a denied read surfaces in
  Athena as `TABLE_NOT_FOUND` while every metadata API call is allowed (inferred from CloudTrail).
- **The SMUS model version page renders two fields of the model card, and no notes.** Its Custom
  metrics shows `training_details.training_job_details.training_metrics` and its Hyperparameters
  shows `...hyper_parameters`; the `user_provided_*` twins, `evaluation_details`, ModelMetrics
  statistics, `MetadataProperties` and `CustomerMetadataProperties` are not shown (probe version,
  2026-10-24). A metric's notes never appear, so anything the approver must see is a row of its own.
  `UpdateModelPackage` takes `ModelCardStatus` with the content only on a package's FIRST card; once
  one exists, both together are a `ValidationException` (`deploy/evidence/VERIFICATION_provenance_card.md`).
- **A containment metric scores a fabricated identifier as a match, and the data is where it comes
  from.** On 2026-10-23 every fine-tuned answer ended in a case number the question never gave
  ("（案件編號 C00089）"): all 234 training answers ended in one, because the CRM appends it to every
  agent reply, and `answer_match` (reference contained in the answer) passed them all. Silver now
  strips a reference the customer never gave (`strip_ungrounded_ids`, AFTER `near_dup`, which keys on
  it), and the pairs it makes identical are kept once (the case number was the only difference between
  template-mates: 283 pairs, 266 distinct). `pipeline/eval/identifier_grounding.py` measures the
  result; bound identifiers by ASCII letters and digits, not `\b`, because a CJK character is a word
  character to Python's `re`. And gate a proportion at a threshold its sample can pass: 120/120 has a
  Wilson lower bound of 0.969, so a 0.99 gate on 120 items is never a decisive pass.
- **An artifact two readers depend on needs a declared shape, and the check belongs to the driver.**
  The eval prompt never named `evaluation/gate.json`, let alone its keys, and four live runs wrote
  four shapes (`gate_results` / `decisions` / `results` / `gates_applied`; `decisive_pass` /
  `pass_decisive`; counts as `"87/120"` or `n` alone). The provenance card and the Outcome tab both
  read it by guessing, so a fifth spelling would have blanked both silently. It is now `gate/1`
  (`pipeline/contracts/gate.schema.json` + `gate_contract.py`), stated in the prompt and checked by
  the driver at `stage_complete`, as the sweep's file is. The check re-derives the verdict instead
  of trusting it: the Wilson interval is recomputed from `k` and `n`, and a null
  `metrics.gate_passed` is refused, because the machine would fail closed on it while the file
  recorded something else. **The bars come from the run's DynamoDB row, not the manifest**: the
  harness role has PutObject on `runs/*`, so a bar read from the manifest is a bar the agent can
  lower first (found by the 2026-10-28 security audit, before this merged). For the same reason a
  borderline's directive must be one the stage-events table says was delivered, NaN is refused
  (`json.loads` accepts it, and a NaN bar passes every comparison), and the four proportion metrics
  cannot be relabelled `band` or `interval`. The other agent-written
  files the card reads (`training/metrics.json`, the answers files) still drift and are read by
  guessing (`METRICS_JOB_KEYS`, `ANSWER_FILES` in `provenance.py`).
- **DeepSeek-R1 output contains `<think>...</think>`** — the data-prep agent strips
  it by default (sequence-level KD keeps final answers only).

## Security rules (non-negotiable)

- This is a PUBLIC repo. **No AWS account IDs, no account-bearing ARNs, no bucket
  names with account IDs, no credentials** — anywhere, including docs, evidence
  files, and commit diffs. Use `<ACCOUNT_ID>` placeholders; deploy scripts substitute
  at run time. `hooks/pre-commit` and `.github/workflows/redaction-check.yml` enforce.
- Least-privilege IAM only. No `*FullAccess` managed policies.
- The VPC is built by `deploy/02_network.py`. Harness configs (`agents/*/harness.json`)
  are PUBLIC-network for iteration speed. VPC-mode harness variants are **not built yet**
  and depend on the S3 skill mirror above (a VPC-mode harness cannot resolve a git skill
  source). Interface endpoints bill **per AZ**, so the script skips all 11 until a
  consumer exists; if you add a service or a subnet, the printed cost is derived from
  `len(INTERFACE_SERVICES) × len(subnet_ids)` and needs no hand-editing.

## Repo conventions

- Every deploy/wiring script is idempotent, supports `--dry-run`, and publishes its
  outputs to SSM under `/llmops/*`.
- Every phase of work ends with a REAL AWS verification, logged in
  `deploy/evidence/VERIFICATION_*.md` and summarized in `docs/TEST_RESULTS.md`.
  "Always invoke before declaring success."
- Docs are bilingual: `X.md` (EN) + `X.zh-TW.md` (繁體中文), updated in the same PR.
- Architecture SVGs are GENERATED (`docs/gen_architecture_svg.py`) and layout-checked
  (`tests/test_svg_geometry.py` — no wire crossings, no wire through a card).
  Never hand-edit the SVGs.
- `PROJECT_STATE.md` records deployed resource names/versions (redacted); update it
  whenever you create or delete AWS resources.
- **Do not stack PRs unless `delete_branch_on_merge` is on.** GitHub retargets an open
  child PR's base to the parent's base only when the parent's head branch is *deleted*
  after merge — the merge alone does nothing. With branches kept, merging a stack
  bottom-up puts every child's merge commit on a branch that was itself merged a second
  earlier: every PR reads MERGED and nothing after the first reaches `main`. That happened
  here twice — 2026-08-11 (#76/#77/#78/#83, unnoticed for a day) and 2026-08-12 (12 PRs
  merged, 1 landed, 28 files missing) — and neither the test suite nor CI could see it,
  because both run on a branch while the defect is a property of the graph *after* the
  merge. Prefer basing every PR on `main`. Check the setting with
  `gh api repos/O/R --jq .delete_branch_on_merge`, and note it is necessary but not
  sufficient: a ruleset that restricts branch deletion suppresses the deletion, and with
  it the retarget.
- `tools/audit_landed.py` is the detector for exactly that: for every PR labelled MERGED
  it asks whether the merge commit — or, for a stack rescued by merging the tip, the head
  commit — is reachable from the default branch, and exits non-zero if either the answer
  is no or *no answer could be obtained*. `.github/workflows/landed-check.yml` runs it on
  every push to `main`, on every PR close, and daily, with `fetch-depth: 0` because a
  depth-1 checkout cannot answer an ancestry question. A merge-commit-only miss is
  reported as **residue**: nothing is lost, but a stack collapsed and was rescued.
