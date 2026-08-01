# Architecture — design rationale

[繁體中文](ARCHITECTURE.zh-TW.md) · [README](../README.md) · [Infrastructure](INFRASTRUCTURE.md) · [Triggers](TRIGGERS.md) · [Test results](TEST_RESULTS.md)

Every decision below was validated by a real invocation on a real AWS account
(evidence in `deploy/evidence/VERIFICATION_phase*.md`). Where a decision was
*changed* by live evidence, the incident that changed it is cited.

![High-level architecture](architecture-high-level.svg)

![Low-level architecture](architecture-low-level.svg)

## 1. Seven harnesses (5 specialists + 1 conductor + 1 auditor), not a mega-agent

The platform is seven AgentCore Harnesses:

| Harness | Role | Tasks |
|---|---|---|
| `llmops_data_prep` | specialist | verify, generate, curate |
| `llmops_finetune` | specialist | prepare, launch, analyze, remediate |
| `llmops_eval` | specialist | evaluate, gate |
| `llmops_deploy` | specialist | deploy, smoke, teardown |
| `llmops_monitor` | specialist | health, sweep, report |
| `llmops_orchestrator` | **conductor** | plan, triage, report |
| `llmops_finops` | **auditor** | reconcile, pricing_refresh, report |

Why not one mega-agent with all the skills and all the permissions:

- **Per-stage skill mounting** — each harness mounts only its stage's skills from
  [MLOps-agent-skills](https://github.com/timwukp/MLOps-agent-skills) (e.g. finetune gets
  `llm-fine-tuning`, `llm-distillation`, `llm-cost-optimization`; eval gets `llm-evaluation`,
  `llm-guardrails`). A mega-agent's context would carry every skill on every turn.
- **Per-stage versioning and endpoint pins** — a change to the deploy agent cannot regress
  the data-prep agent; each harness versions and pins independently.
- **Per-stage least-privilege IAM** — small blast radius. Phase 4's teardown proved the value:
  the deploy agent's role denies `List*`/`DeleteModel`, so it planned teardown around
  known-name deletion and *flagged* what it couldn't do rather than silently claiming it.
- **Per-stage evaluations** — online evals attach per harness, so quality regressions
  localize to a stage.
- **Independent honesty** — Phase 2 pilot: `llm-cost-optimization` is deliberately NOT
  mounted on data-prep; the agent flagged that it couldn't cross-check pricing instead of
  guessing. Separation makes that boundary real.

The conductor sits ABOVE the state machine, not inside it: it parses natural-language
goals into run plans, dispatches runs, triages escalations first-line, and synthesizes
cross-run reports. It does not execute pipeline stages and does not schedule intra-run
steps. The config comment says it best: 樂譜不即興，指揮家不吹每個音符 — the score doesn't
improvise, and the conductor doesn't play every note.

## 2. Deterministic spine, agentic workers

The stage DAG (data-prep → finetune → eval → deploy → monitor) never changes per run —
it needs zero LLM judgment. So orchestration is a **Step Functions Standard state machine**,
and intelligence lives *inside* each stage. An LLM deciding "what stage comes next" would
add nondeterminism, cost, and failure modes to the one part of the system that benefits
from having none.

The state machine (`orchestration/state_machine.asl.json`) has **8 harness-task states
on the happy path** — each a `waitForTaskToken` Lambda invocation of the harness driver
— plus the loop-only `RemediateFinetune`:

```
DataPrepGenerate → DataPrepCurate → FinetuneLaunch → FinetuneAnalyze → EvalGate
                                                        │ (gate fail)
                              RemediateFinetune ←───────┘   … then, on gate pass:
                                                     Deploy → SmokeTest → Teardown
```

plus the control states that route between them: `QualityGateChoice` (gate pass → Deploy,
else remediation), `RemediationChoice` (iteration < 3 → remediate, else escalate),
`IncrementIteration` (Pass), and the terminals `Complete` (emits `PipelineCompleted`),
`EscalateFail` (emits `EscalatedToHuman`), and `Fail`.

Two spine details that are policy, not plumbing:

- **Both records are closed on both paths.** A run writes two records — its own row in
  `llmops-pipeline-runs` and the originating task's row in `llmops-tasks` — and each path
  has now been caught closing only one of them. The failure path (`EscalateFail` →
  `MarkRunFailed` → `MarkTaskFailed`) closed the run but not the task, which left
  `task-58ecde82adcd73bf` at `dispatched` for a day. Then the success path: `runs.status`
  was only ever written `running` (start-pipeline), `escalated` (the driver), or `failed`
  (`MarkRunFailed`) — **nothing wrote `completed`**, so every successful run stayed a
  zombie, exactly what `MarkRunFailed` exists to prevent on the other branch. It was
  invisible because until `run-20260801T062313Z-4d3e2e69` **no execution had ever
  succeeded** (6 failed, 1 aborted); that run's task row closed correctly at 06:34:43Z
  while its run row still read `running` five hours later. `Complete` → **`MarkRunDone`**
  → `MarkTaskDone` closes it, conditional on `attribute_not_exists(status) OR status =
  running` so it can never overwrite a richer verdict, and its `Catch` falls through to
  `MarkTaskDone` for the same reason `MarkRunFailed`'s does — failing to close one record
  must not leave the other open. Guarded by property-based tests that derive the closers
  from the ASL rather than naming them.
- **`Teardown` always runs after deploy** — even when `SmokeTest` fails, its `Catch`
  routes to `Teardown` first. Orphaned endpoints are the #1 cost risk (Phase 4 found an
  unrelated endpoint in the account that had been InService since 2024-04).
- The spine is also the **deterministic fallback**: during a transient Bedrock outage in
  Phase 3, the orchestrator submitted training job `-r5` directly (tagged
  `launched_by: orchestrator-fallback-bedrock-5xx`) while the agent was unreachable.

## 3. The inline-function contract

Agents don't "report" in free text — the driver only trusts structured inline-function
calls, which are the sole channel by which an agent affects the pipeline.

**Worker contract** (all 5 specialists):

| Function | Meaning | Driver behavior |
|---|---|---|
| `stage_complete` | stage done, here are outputs + metrics | **trust-but-verify**: `head_object` every claimed `s3://` output; missing outputs → the call is *rejected* back to the agent ("write them and call stage_complete again"). The driver — never the agent — writes the canonical run report. `outputs: []` is a legitimate success. |
| `job_launched` | long job launched (SageMaker training) | launch-and-release: park the Step Functions task token in DynamoDB keyed by job name; release the session (§4) |
| `checkpoint` | turn budget nearing, progress persisted | re-invoke the same session to continue (self-reinvoke if the Lambda itself is near its limit) |
| `escalate_human` | out of budget or out of authority | SNS notification, run marked `escalated`, `EscalatedToHuman` event, task token failed |

**Conductor contract** (`llmops_orchestrator`): `launch_run` (dispatch a planned run via
start-pipeline), `resolve_escalation` (first-line triage within policy: relaunch a stage
with adjusted params, skip, documented retry), `page_human` (ONLY for decisions above its
authority — budget overruns, shared-resource deletion, business tradeoffs — with a
decision brief: situation, options, recommendation), `write_report` (publish cross-run
operations synthesis), plus `checkpoint`.

**A verdict is delivered or visibly undeliverable — never silently filed.** The answer
channel (`put_directive` → the `checkpoint` branch's `take_directive`) has exactly ONE
reader: a *live* driver invocation. So delivery is not a property of the write, it is a
property of whether anyone will ever read it — and `resolve_escalation` used to return
`{"status": "resolved"}` either way. That is why the data-prep budget escalation sat
unanswered for three days: `run-20260729T104648Z-41631739` was already `escalated`, its
task token failed by `handle_escalate` and its execution FAILED at 11:19:55Z, so triaging
it would have reported success and changed nothing. The tool that existed to answer the
escalation could not answer *that* escalation, and said so to no one. The same shape as
the stranded task token in §4: **the write was authorized, and unreachable.** Now
`put_directive` consults the run's status first; a verdict for a run in a terminal state
is still written (a decision is evidence even when nobody acts on it) but flagged
`deliverable: false`, and the call is *rejected back into the same turn* naming the paths
that can still act — `launch_run` to relaunch the work carrying the adjusted params, or
`page_human`. An unknown or unreadable run row counts as reachable on purpose: the defect
being fixed is a silent no-op, and withholding a verdict on a transient DynamoDB error
would invent a second one in the same direction.

**An escape hatch must be serviced on the path that uses it.** That rejection names two
paths, and one of them was not wired. `page_human` was declared on the orchestrator
harness from Phase 5 on and handled only by the console's chat worker — but a triage is
never a chat: an `EscalatedToHuman` event routes to the *driver*, and there `page_human`
fell through to the unknown-tool branch and answered `{"status": "unsupported"}`. No SNS,
no event, no owner told. Measured live on 2026-08-01 at 13:45Z, with the fix above already
deployed: the conductor was correctly told its verdict was undeliverable and to use
`launch_run` or `page_human`, so it re-called `resolve_escalation`, was rejected again,
wrote `plan.json` and `relaunch-plan.json` to S3, and the turn ended — **zero runs
dispatched, zero pages sent.** The previous drift guard passed because it asked whether a
declared tool was serviced *anywhere*; the console qualified, and only the driver runs a
triage. The guard is now per-path and derives the tool list from the prompt's own triage
clause, so a protocol that grows a third exit cannot leave it half-wired. A page is also
now rejected unless it carries both `situation` and `recommendation`: paging an owner with
the problem and none of the analysis leaves them exactly where they started.

**A prefix is not a filter.** The verdict channel above parks directives under a
`directive#` sort key, and the constant's own comment claimed the prefix kept them "out of
the timeline the console renders". Neither console reader filtered on it — and the prefix
made the outcome *worse* than harmless rather than merely undelivered. `"d" > "2"`, so
every `directive#` row sorts after every ISO-timestamped stage event, landing exactly in
the window the operator sees (`evs.slice(-25)`); and a directive row carries no `detail`
attribute, so each rendered as a blank line. Ten parked verdicts on a run with thirty
events therefore showed ten blank rows *and pushed the ten newest real events off the
screen* — the timeline degraded in proportion to how much triage a run had needed. The fix
is two bounded queries rather than one filtered list, because a single `Limit`-ed query
spends its budget on directives before the rows reach the Lambda: filtering afterwards
would yield a short timeline with nothing to indicate anything was dropped. The event range
is bounded on `"A"` — stage-event keys are ISO timestamps and so begin with a digit, while
every non-event row uses a named `word#` prefix — and **not** on `directive#`, which would
have fixed the symptom and re-armed the defect for the next prefix added (`audit#` and
`checkpoint#` sort *before* `directive#` and would have been served as stage events).
Directives are returned and rendered as their own section carrying `deliverable` /
`delivered`, because a verdict that could never be read must not look like one an agent
acted on — the indistinguishability that let the data-prep escalation read as answered for
three days.

If a turn ends *without* an inline-function call (models sometimes narrate completion but
skip the structured call), the driver re-asks up to 2 times in the same session, then
fails the stage as `MissingStageComplete` — narration is never promoted to success.

## 4. Launch-and-release with EventBridge wake

A harness turn is bounded (~14 min); a training job runs for hours. The rule: **a harness
never waits on a job**. The finetune agent launches the SageMaker job, calls
`job_launched`, and its session is released. The chain that resumes the pipeline:

1. Driver parks the Step Functions task token in DynamoDB (`llmops-pipeline-runs`,
   GSI `job_name-index`), keyed by job name.
2. The job's terminal state fires the EventBridge rule on "SageMaker Training Job State
   Change" (Completed | Failed | Stopped) → `llmops-resume-pipeline` Lambda.
3. The Lambda looks the run up by job name, emits `ModelTrained` / `PipelineFailed`,
   settles the token (`send_task_success` / `send_task_failure`), and **removes the token**
   so a duplicate EventBridge delivery cannot double-settle. The removal is **isolated
   from the settle**, because a token Step Functions has already discarded is stale data,
   not a pending obligation: on 2026-07-29 the clear failed `AccessDenied`, and the ~5
   EventBridge retries that followed all failed *earlier*, at the settle, with
   `TaskTimedOut` ("Provided task does not exist anymore") — so none of them ever reached
   the clear, and `run-20260729T104648Z-41631739` held a `task_token` for an execution
   that had ended at 11:19:55Z. Granting the missing IAM was necessary and not
   sufficient; **an IAM grant fixes what is forbidden, never what is unreachable.** Only
   "the task is gone" is absorbed — a throttle or 5xx still reraises *without* clearing,
   since that token is the pipeline's only way to learn the stage finished — and a
   failure of the clear itself is raised rather than swallowed, because that is precisely
   the failure whose traceback was about something else.
4. `FinetuneAnalyze` then runs in a **fresh session**, reconstructing all context from AWS
   state (describe-training-job + S3 + manifest).

Live-verified in Phase 3 (two resume-Lambda invocations observed: one on an early failure,
one on the successful completion; 1.5 s duration, 0 errors) and again hands-off, twice, in
Phase 5.

## 5. Manifest as the single source of truth

**State lives in `s3://<bucket>/runs/<run_id>/manifest.json` + DynamoDB, never in the
session.** The start-pipeline Lambda seeds the manifest (run params, models, gates,
budget); every stage reads it and appends its entry; `remediation_history` is
append-only. Consequences, all live-proven:

- Sessions are disposable: Phase 3's post-training `analyze` ran in a fresh session with
  zero context loss; Phase 2 survived 3 client-side stream disconnects (including a full
  laptop-close) with zero lost work.
- Scaling is a parameter change, not new engineering: the 24-task Phase 2 run and the
  2-sample Phase 5 mini-run differ only in manifest params.
- Learnings persist where the next run will look: the Phase 2 agents recorded token-budget
  and checkpointing findings directly into the manifest.

## 6. Shared BYO Memory — cross-run learning

One AgentCore Memory (`llmops_shared_memory`, strategies **SEMANTIC + EPISODIC**) is
shared by all seven harnesses, wired post-create by `deploy/04_wire_memory.py`. Per-agent
`actorId` (= harness name) partitions namespaces while retrieval can still cross-read
shared facts. USER_PREFERENCE/SUMMARIZATION strategies are deliberately skipped — there
is no human user in the loop.

This is the mechanism by which run-N learnings reach run-N+1, and it is proven: in
Phase 5's e2e run, the finetune agent launched its training job **first-try** using the
floors-only-requirements + torch-2.6-DLC recipe learned in Phase 3's remediation gauntlet
— a recipe that had cost 5 failed jobs to discover.

## 7. Fail-closed gates

Quality gates default to FAIL. This was made strict by a live defect (Phase 5, e2e
iteration 4): a mini-run's eval agent emitted `gate_passed: null` + `needs_human: true`,
and the old default-True coercion *promoted a null to a pass*. The fix in the driver:

- On an eval gate task, `gate_passed` is `metrics.get("gate_passed") is True` — absent,
  null, or anything non-True means NOT passed. Regression-tested.
- The gate held in Phase 4 the way that matters most: the model genuinely failed
  (0/16 twice, `closed_think_rate` 0%) and the verdict **stood** — it was not "fixed"
  into a pass. A gate you can talk your way past is not a gate.

## 8. The remediation loop (≤ 3 iterations)

`QualityGateChoice` fail → `RemediationChoice`: while `iteration < 3`,
`IncrementIteration` → `RemediateFinetune` → back to `FinetuneAnalyze` → `EvalGate`.
Budget exhausted → `EscalateFail`. The same budget appears inside the agents' task
prompts ("diagnose, fix, retry — max 3; then `escalate_human`"), so the machine-level
and agent-level budgets agree.

The loop's honest edge case is its best evidence (Phase 5, run 5): the eval agent
returned `FAIL_CLOSED_NO_INPUT` (no quality signal exists at 2-sample scale), the machine
correctly armed `RemediateFinetune` — and the finetune agent answered
`REMEDIATE_PREMISE_INVALID — no quality signal to remediate` and escalated instead of
burning iterations on an unfixable premise. Honest-over-busy is a design requirement,
and the loop's exit for it is `escalate_human`.

## 9. Model failover is a design layer, not an emergency measure

Live-established (Phase 5): vendor model quotas are a HARD constraint — even AWS-internal
accounts are rate-limited by the model provider, and a multi-agent platform is its own
token-flood generator (6 harnesses × agent loops × long streams). ~12 Fable 5 5xx bursts
recurred across a single day. Design rules (full text in [AGENTS.md](../AGENTS.md)):

1. Every harness has a fallback chain: `global.anthropic.claude-fable-5` →
   `global.anthropic.claude-opus-5` (same family, zero prompt changes). Hot-swap via
   `UpdateHarness`, ~15 s to READY; sessions survive the swap.
2. The "switch" signature: repeated `InternalServerException`/`ServiceUnavailableException`
   from ConverseStream while a direct single-shot probe of the same model succeeds —
   quota pressure, not an outage (it never surfaces as an explicit ThrottlingException).
3. Mixed allocation spreads quota pressure: premium tier for judgment-heavy agents
   (orchestrator, eval), Opus 5 for process-execution agents (data-prep, deploy, monitor).
4. The driver detects model-5xx during stream salvage and hot-swaps to the fallback,
   emitting an informational failover event (`_maybe_failover_model` in
   `orchestration/harness_driver/handler.py`); full automated-failover hardening is Phase 6.

## 10. The driver's turn-continuation design (900 s Lambda vs 840 s turns)

The harness driver Lambda is the bridge between Step Functions and a harness: one
invocation streams `InvokeHarness`, services the toolUse ⇄ toolResult protocol, verifies
outputs, and settles the task token. The arithmetic problem: the Lambda's hard ceiling is
**900 s** and a harness turn budget (`timeoutSeconds`) is **840 s** — so only ONE turn
fits per invocation. Live evidence forced the design (Phase 5, e2e iteration 4):
`Sandbox.Timedout` killed a run whose agent had *finished its work but never got a turn
to report it*.

The fix: **between-turn self-reinvoke**. Whenever the loop would start another turn
without enough remaining time, the driver asynchronously re-invokes itself carrying a
continuation payload (pending content + retry/re-ask counters); the deterministic session
id and the task token survive across invocations, so the conversation resumes exactly
where it stopped. Other production patterns baked into the same loop, each from a real
failure: `read_timeout=870, retries=0` on the AgentCore client (default 60 s kills long
streams; auto-retry would silently re-run a whole agent turn), and a one-shot same-session
salvage retry when a stream dies mid-turn.

## 11. VPC posture for production

Harness configs (`agents/*/harness.json`) run PUBLIC-network for iteration speed. The
VPC itself is built by `deploy/02_network.py` and the Lambdas can run **VPC-isolated with
interface endpoints — no internet egress**.

**Not yet built** (tracked as the s3-skill-source work): the VPC-mode harness variants,
and the *switch* of the sources themselves. The mirror they require now exists —
`ensure_skills` in `deploy/03_storage.py` derives what to mirror from the harness configs,
validates each `SKILL.md`'s frontmatter before uploading, and reads every one back, with
the harness role granted `GetObject` + `ListBucket` on `skills/*` and no write. All
**19 skill sources across the 7 harnesses are `git` sources today; none are `s3`** —
verified by
`tests/test_docs_claims.py::test_the_skill_source_claims_match_the_harness_configs`,
which reads the configs rather than trusting this paragraph. Earlier revisions of this
file described `agents/*/harness.prod.json` and `deploy/05_mirror_skills.py` as existing;
both paths do not exist, and never have in any branch, so that was a design being read as
a shipped feature.

Two forcing functions make the mirror a prerequisite for VPC mode rather than an
optimization:

- VPC-mode harnesses can't reach GitHub, so a git skill source cannot resolve at all —
  and a bad or unreachable source fails at **session start**, not at `UpdateHarness`, so
  the harness would be accepted and then fail every invocation. That asymmetry also makes
  the mirror's *permissions* part of the prerequisite rather than a follow-up: the fetch
  runs as `llmops-harness-execution`, so the deployer's own successful read-back of an
  uploaded object proves nothing. Measured with `simulate_principal_policy` after the
  first upload, that role was **implicitly denied** on the very keys it would have been
  asked for.
- It is also correctness, not just connectivity: the git skill source reads the default
  branch only (there is no branch field), so main-branch drift in the skills repo
  silently changes production agent behavior. An S3 snapshot pins what agents run.

## 12. The auditor is outside the state machine, and read-only

`llmops_finops` is the only harness with no place in a run's stage sequence, and that follows
from the shape of its job rather than from taste.

`llmops_monitor` runs *inside* the state machine: per-run, within a run's lifetime, answering
"is the endpoint alive now". Reconciliation is the opposite shape on all three axes — it runs
**after** the run is over (Cost Explorer lags ~24 h), it spans **many** runs, and it answers to
the project rather than to any one run. A run that finished yesterday has no live agent to
attribute today's settled bill, so putting this in `monitor` would mean a per-run agent reaching
across other runs' data.

So it sits beside `llmops_orchestrator`, above the spine: **the conductor decides what to spend,
the auditor reports what was spent.**

Its IAM is read-only on billing (`ce:Get*`, `pricing:*`, `budgets:ViewBudget`) and it has no
authority to terminate anything. Two reasons, and the second is the load-bearing one:

- An auditor must not be able to change what it audits.
- Stopping a run is the orchestrator's authority via `page_human`. Giving kill rights to the
  component whose job is to *observe* puts spend-control authority in the wrong place — and an
  auditor that can act on its own findings has no independent check on those findings.

The $2000 approval gate lives in the console and `cost_model.py`, not in the auditor, for the
same reason: the thing that measures spend is not the thing that authorises it.
See [COST.md](COST.md).

Least-privilege IAM throughout (no `*FullAccess`), all resources scoped to `llmops-*`,
and this being a public repo: no account IDs anywhere — deploy scripts substitute
`<ACCOUNT_ID>` at run time, enforced by a pre-commit hook and CI redaction scan.
