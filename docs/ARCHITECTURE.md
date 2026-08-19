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

The state machine (`orchestration/state_machine.asl.json`) has **12 harness-task states
on the happy path** — each a `waitForTaskToken` Lambda invocation of the harness driver
— plus the loop-only `RemediateFinetune`, the audit-only `DataAudit`, and two entries that
start partway down this same path: `eval_only` at the eval stage and `deploy_only` at the
serving stage:

```
DataPrepGenerate → DataPrepCurate → FinetuneLaunch → FinetuneAnalyze → EvalGenerate → EvalScore → EvalGate
                                                        │ (gate fail)
                              RemediateFinetune ←───────┘   … then, on gate pass:
             Deploy → SmokeTest → MonitorHealth → Teardown → MonitorReport
```

**Four entry modes, decided by one `Choice` at `StartAt`** reading `pipeline_mode` out of
the execution input (it cannot read the manifest from S3, which is why the mode rides in the
input at all). `full` is the `Default` above. `data_audit` is the conductor's cheap starter:
audit the customer's data and stop before any GPU exists. **`eval_only`** enters at
`EvalGenerate` to re-judge an artifact an earlier run already produced and paid for, and
`EvalOnlyStopChoice` stops it at the gate verdict — a pass does **not** reach `Deploy` and a
fail does **not** reach `RemediateFinetune`. Both halves of that are deliberate: re-measuring
an artifact is not approval to serve it, and there is no finetune stage in this manifest to
remediate, so the remediation path would launch GPU training in the one mode whose entry
exists to avoid it. The mode is refused at dispatch unless the plan names both
`model_artifact_uri` (nothing in this run can produce one) and `customer_eval_uri` (the 10%
val split the eval agent normally falls back to is written by the `curate` task this mode
skips) — `MODE_REQUIRED_PARAMS` in start-pipeline, because a run that can only escalate
should never get a run id, a manifest and a `PipelineStarted` event first.

It exists because of r6c: an 8B run produced a 12.2 GiB model, the reformed judge metric had
to re-score it, and with only `full` and `data_audit` the only way to do that was a script in
someone's working directory — unversioned, unaudited, and invisible to the runs table.

**`deploy_only`** enters at `Deploy` and runs the serving tail —
`Deploy → SmokeTest → MonitorHealth → Teardown → MonitorReport` — against an artifact an
earlier run already paid for. It exists because **those five states have never executed
once**. Measured 2026-08-15: 38 rows in `llmops-pipeline-runs`, and `deploy` / `smoke` /
`teardown` have emitted **zero** stage events, ever; the newest `deployment/` object in S3
is from 2026-07-29, written by a hand-run script. The cause is not a bug in those states, it
is arithmetic: `QualityGateChoice` is `Deploy`'s only other entrance and `gate_passed=true`
has never been produced, so the last five states of a 29-state machine are structurally
unreachable and every defect in them is undiscovered. Every recent run has died on a
different one-off defect of the same shape — *a capability the docs, the prompt and the IAM
policy all describe, that nothing can actually reach* (five separate missing IAM grants for
calls the code always makes) — and that class is only findable by arriving. So this mode is
a **rehearsal lane**: one `ml.g5.xlarge` endpoint-hour, under $5, against the real AWS
control plane, versus ~$50 to reach the same states through a full run that has never
managed it. It is cheap and it needs no faked gate verdict because of a property of the
states themselves: their Payloads carry only `run_id` / `manifest_uri` / `stage` / `task` /
`harness_id` / `iteration` and read neither `$.evaluation` nor `$.finetune`.

It is also **a deploy with no gate verdict**, which is the one thing `EvalOnlyStopChoice`
refuses to allow, so its legitimacy rests on three boundaries that each have a guard test:

1. **The top-level `Choice` is the only entrance** — the dispatch *is* the approval, exactly
   as in `eval_only`. `Deploy`'s entrants are pinned to `{PipelineModeChoice,
   QualityGateChoice}`, and a mode read *after* dispatch (`EvalOnlyStopChoice` does one,
   legitimately) may only end a run early, never start a stage.
2. **The artifact must be named.** `MODE_REQUIRED_PARAMS` demands `model_artifact_uri`, and
   `MODE_REQUIRED_ROLES` — a separate table, because `DEFAULT_MODELS` always supplies a
   silent `student` so a presence check on it can never fail — demands that a human
   explicitly *chose* the base model those adapters merge into. A merge onto the wrong base
   is only discoverable after the endpoint is paid for.
3. **The registry stays pending.** `model_package_approval_status` defaults to
   `PendingManualApproval`; a rehearsal must never leave an `Approved` model package behind
   because three canary prompts returned non-empty strings.

`pipeline_mode` is now written to the run row as well as the manifest, so a rehearsal deploy
and a gated deploy are never confused in the table every operator, cost review and triage
reads first — and so the endpoint-hours bill apart.

Building it surfaced two latent defects of exactly the class above, both in
`agents/deploy/harness.json`, both invisible because nothing had ever reached the state that
would hit them: the deploy prompt **never said where the model artifact comes from** (not in
`deploy_only`, not in a full run either — it said "merge the fine-tuning adapters into the
base weights" and left the agent to find them, which is how a run ends up serving a
stranger's weights while every log reads as success), and its smoke task told the agent to
register the model with the approval status "per params" while
`model_package_approval_status` existed in no `DEFAULT_PARAMS`, no console form and no test.

The mode is **not** a way around the gate. `gate_passed` having never been true is a separate,
unsolved problem — the transfer floor r6c/r6d measured, not plumbing — and nothing here
softens the gate to make this lane work.

The two monitor states are placed by the shape of their work, not by taste.
**`MonitorHealth`** must read CloudWatch *while the endpoint exists*, and `Teardown`
deletes it on every path including `SmokeTest`'s `Catch` — after the delete,
`GetMetricData` returns an empty series indistinguishable from a healthy idle endpoint, so
the window between those two states is the only one in which the question can be answered
at all. It gates nothing: its `Catch` also goes to `Teardown`, because a metric read that
fails must never strand the endpoint it was watching, and an orphaned endpoint bills
whether or not we managed to measure it. **`MonitorReport`** runs after `Teardown` because
it consolidates the *finished* manifest — a report composed earlier would omit the teardown
it exists to confirm — and its `Catch` goes to `Complete`: the narrative is a deliverable,
the run's terminal state is a fact.

The third monitor task, **`sweep`**, is deliberately *outside* the state machine, on an
08:00 UTC schedule (`llmops-monitor-sweep-daily` → `llmops-monitor-sweep`). It hunts
endpoints left running by *other* runs, including runs that crashed and so never reached
any state that could have looked — a run-scoped agent cannot answer for other runs. The
account proves the point: the one standing endpoint it carried,
`jumpstart-dft-hf-asr-whisper-large-v2`, was InService from 2024-04-11 until its deletion on
2026-08-02, with no `project` tag at all, so no run was ever going to be responsible for it
and its `ListTags` grant must be account-wide (`Resource: "*"`). The grant stays wide now
that the endpoint is gone, for the reason it was widened in the first place: the sweep
exists to find the *next* unclaimed resource, and a scope restricted to what we already call
ours is what let this one bill for its whole 843-day life unnoticed. The boundary is **read account-wide, mutate
`llmops-*` only** — `ListEndpoints`/`ListTags`/`DescribeEndpoint`/`DescribeEndpointConfig`
on `"*"`, every mutation still scoped: the sweep can fully characterise an orphan it cannot
touch. The first live sweep is why the line falls at read-vs-mutate rather than at
`Describe`. It found that endpoint, then filed its own permission gap — `DescribeEndpoint`
was scoped to `endpoint/llmops-*`, so the instance type behind the one endpoint it flagged
was unreadable and its headline number (~$1106/month, ~$30.6k since 2024-04-11) went out as
a guess at the JumpStart default. **A cost finding whose figure is an assumption is one the
owner can correctly dismiss**, and the figure is the entire value of the finding. The
tempting fix — widening the lifecycle statement — would have handed `DeleteEndpoint` over
the whole account to an agent whose prompt forbids deleting anything.

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
- **Closing a run and MINTING one are the same DynamoDB call.** `update_item` is an
  upsert: on a key with no row it creates one from the key plus whatever `SET` writes. So
  the driver's `handle_escalate`, whose only intent was "mark this run escalated", filed a
  brand-new run for every escalation by something that is not a run — a two-attribute row
  `{run_id, status: escalated}` with no `created_at`, no `trigger_source`, no `iteration`.
  Live: **`sweep-2026-08-01`**, from a scheduled orphan-endpoint sweep. The sweep Lambda is
  not the culprit and could not have prevented it: it writes its own bookkeeping row to the
  stage-events table and its docstring spells out why a sweep must never read as a run
  (the console would list it, the auditor would reconcile its cost, and the run count every
  doc quotes would rise by one a day). The driver wrote one on its behalf, through a path
  the sweep does not know exists. The first fix for this named the one non-run invoker known
  at the time — `stage == "finops"` — which is precisely why the sweep, added later under
  its own synthetic `sweep-<date>` id, walked back into it; triage's `triage-<subject>` is
  the same shape. So the guard is not a third entry in a list of stages that are not runs
  but a `ConditionExpression: attribute_exists(run_id)`: **only `start_pipeline` creates run
  rows, so "a row exists" is the question, and the table itself answers it.** A rejected
  condition is the answer and returns quietly; every other error still raises, because
  absorbing a throttle here would leave a run that genuinely escalated at `running` — the
  zombie of the bullet above, reintroduced by the fix for the bullet below it. And the row
  write turned out to be standing in for a record that was never written: `handle_escalate`
  emitted **no** stage event, unlike `handle_page_human`, so an escalation has never
  appeared in the timeline the console renders from `llmops-stage-events` — for a real run,
  `runs.status` was the only durable trace. Declining the row would have made that "no trace
  anywhere" for a sweep, so the escalation is now recorded in stage-events on **both** paths,
  carrying `run_row` to say which one ran. That write is bookkeeping and is wrapped: a
  failing events table must never withhold the SNS page or the `EscalatedToHuman` event.
- **An escalation's channels are independent, and the one that reaches nobody must not be
  the gate.** `handle_escalate` notifies four ways: SNS to a human, a stage event for the
  console timeline, `EscalatedToHuman` on the bus for the conductor, and `send_task_failure`
  to release the state machine. The SNS publish was the **first** statement and unwrapped,
  so a failed publish took all three of the others with it — including the settle, leaving a
  live task token on a run that had already escalated, freed only by the stage's own timeout
  (86400s — a full day — on every long-work state since 2026-08-03). That is the worst
  possible thing to gate on
  *this* call: **`llmops-escalations` had zero subscribers** when this was found, so SNS was
  the one channel already known to deliver to no one. `ensure_topic` in
  `deploy/03_storage.py` reports it as
  `NO SUBSCRIBERS — every escalate_human call publishes into the void` rather than calling
  the topic healthy, because a deploy cannot invent an address; the fix is
  `--escalation-email <addr>`, supplied 2026-08-02; `PendingConfirmation` is now `false`, so
  the topic has one confirmed recipient. Getting there took two steps, not one, and
  `ensure_topic` reports them separately for that reason: an email subscription exists but
  delivers nothing until the recipient clicks the link, which is the same silence one step
  further along, and no deploy can click it for them. The ordering below is therefore still
  required, not superseded: a channel's audience can go back to zero without any code
  changing. Each notification is now wrapped and logged on its own, in ascending
  order of what it costs to lose: SNS, then the timeline row, then the bus event, and the
  token settle last so it happens even when every notification failed.
- **A readiness checklist must be derived from the prompt, not copied from it.** The console's
  Data-readiness panel exists to show, question by question, what the orchestrator's consult
  protocol told the agent to answer — and which answers are still missing. Its guard restated
  seven paths and asserted the console contained them, so the test agreed with the console and
  with itself while the prompt's `data` block specified **nine**. Live, the panel was missing
  `datasheet.provenance` (a license means little without the origin it applies to) and
  `readiness_report_uri` — the pointer to the Data Readiness Report, which is where the
  audit's PII scan lands. A customer could therefore read a complete-looking panel, see
  `PII disposition: redacted` as a claim in the plan, and have no link to the one artifact
  that examined the data. The panel and its guard now both come from
  `agents/orchestrator/harness.json`: `_prompt_data_block_keys()` in
  `tests/test_console_tasks.py` parses the block out of the prompt, and a second test asserts
  that the derivation is still a derivation rather than a fresh hardcoded list. Same rule as
  the documented-test-count guard — when the source of truth is a model prompt, parse the
  prompt. (`renderReadiness` in `frontend.html` is data-driven from the API's `fields`, so the
  two restored rows needed no frontend change.)
- **Nothing scanned the customer's data, and every signal said otherwise.** The readiness
  panel above links the Data Readiness Report, whose PII section is a *heuristic regex* pass —
  the data-prep prompt says so in as many words. Anyone checking whether more than that
  existed found a Macie session `ENABLED` and a COMPLETE classification job, which reads as
  yes. That job was `ONE_TIME`, created **2021-02-23**, named 25 unrelated buckets, and
  processed **0 objects**; `customer-data/` was scanned by nothing. `ensure_pii_scan` in
  `deploy/03_storage.py` now answers the real question on its own line of the deploy output,
  and `macie_job_covers()` decides it from the bucket list **plus the scoping** — a job can
  name our bucket and still read only `runs/`, and a `bucketCriteria` job is reported as
  undecidable rather than credited. Creating the job is opt-in (`--enable-pii-scan`): a
  `SCHEDULED` job is recurring per-GB spend, and starting it silently is the billing analogue
  of a silent security downgrade. Two API constraints shape it, neither documented:
  `UpdateClassificationJob` takes only `(jobId, jobStatus)`, so a job's scope is immutable and
  the wrong one must be cancelled rather than converged; and `CreateClassificationJob`'s
  `clientToken` means a repeat creates a *second* scanner, so idempotency comes from looking
  up our own job by name. The finding that made the rest matter: the harness execution role
  had **implicitDeny** on every `macie2` read, so the scan would have billed and stayed
  invisible to the very agent that writes the report — `MacieFindingsReadForDataAudit` fixes
  that, read-only in both directions (no job creation, no session disable), and the audit
  prompt must now state *"no Macie classification job covers this data"* whenever none does.
- **The system prompt is resent uncached on every model round-trip, and the two ways to cache
  it both silently discard harness state.** A measured consult turn: `wall=59.0s ttft=26.4s
  rounds=2 model_ms=52030` — 88% of the wall clock is the model, and `in_tok=31691` over two
  rounds is the ~11 KB prompt paid for twice. InvokeHarness has no caching field, but
  `bedrockModelConfig.additionalParams` forwards raw to ConverseStream, so a `cachePoint` gets
  through and demonstrably works (`cacheWriteInputTokens 3568` → `cacheReadInputTokens 3568`).
  It is still the wrong lever: `additionalParams.system` **replaces** the harness prompt (the
  same agent answered `NO-PROTOCOL` to a question it had just answered correctly, while input
  tokens *fell* 10840 → 6644), `additionalParams.messages` **replaces** session history (a
  codeword from the previous turn came back `NONE`), and echoing `GetHarness`'s prompt back
  loses the skills manifest the runtime injects but the control plane never returns — 1148
  tokens, after which the agent listed 2 of its 4 skills. Every wrong path reports fewer
  tokens and a cache hit. Until InvokeHarness exposes caching, the lever is **fewer
  round-trips**, not cheaper ones.
- **A mounted skill the prompt does not name is a skill the agent is not told to consult.**
  The orchestrator mounted four and its prompt named two; the unnamed
  `llm-data-preparation` is the methodology for step 0 of its own consult protocol. The mount
  guard passed because the mount was real. The prompt is what carries "consult them before
  acting", so the guard now derives the names from each harness's `skills` list in both
  directions.
- **An "append-only" log implemented as read-modify-write is one transient error from
  erasure.** The Tasks tab's S3 audit copy re-read `transcript.jsonl`, concatenated, and
  put it back, with the read's failure swallowed as *"no file yet"* — so a single 503
  replaced the whole history with the newest lines, and two writers (`close_task` is
  allowed mid-turn) silently lost one another's messages. It now writes **one timestamped
  object per append**: no read, nothing to overwrite, keys sorting into chronological
  order. Two related fixes in the same twelve lines: the 8000-character cap was applied
  *before* the DynamoDB/S3 split, so the "full-text" copy was a truncated copy of a
  truncated record (live, an **assistant** reply — the kind an acceptance is signed
  against — sat at exactly 8000 in both); and the audit write was unwrapped, so one S3
  failure skipped the `PlanAccepted` event and the worker enqueue that followed it,
  stranding a KMS-signed acceptance at `accepting`. Nothing reads this artifact back,
  which is exactly why nothing noticed — verify a write-only artifact by reading it.
- **The gate's input is produced on the path that reads it.** `EvalGate` applies thresholds
  to `evaluation/report.json`; until `EvalGenerate` was inserted above it, **nothing
  dispatched the task that writes that report**. Both `evaluate` and `gate` are declared in
  the eval harness prompt; only `gate` appeared in the ASL, so the only eval task the
  pipeline could run read an input no path produced — the pipeline had never traversed
  `evaluate → gate`. The gate's fail-closed rule (`metrics.get("gate_passed") is True`,
  correct and load-bearing elsewhere) hid it perfectly: **a gate that failed because its
  report was never generated reads exactly like a gate that failed because the student was
  bad.** Phase 4's FAILED verdict stands only because eval was run *directly*, outside the
  machine; through the machine the same verdict would have been unfalsifiable. The
  completion now also emits `ModelEvaluated`, which was in the event vocabulary
  (`pipeline/contracts/events.py`) and emitted by nothing — the same absence from the other
  side. Guarded by tests that diff every task declared in a harness prompt against every
  task any dispatcher actually sends, with the remaining orphans an explicit allowlist
  rather than a count.
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
| `checkpoint` | turn budget nearing, progress persisted, **or blocked on a human decision** | re-invoke the same session to continue (self-reinvoke if the Lambda itself is near its limit). This is the platform's only **live** human-in-the-loop pause: the driver returns any parked `{"status": "directive", ...}` on the next turn, so a blocked agent keeps its run alive by checkpointing, not by escalating. |
| `escalate_human` | out of budget or out of authority | **terminal**: SNS notification, run marked `escalated`, `EscalatedToHuman` event, task token failed → `EscalateFail` → `Fail`. `escalated` is in `UNREACHABLE_RUN_STATES`, so a directive sent afterwards is recorded for audit and reaches nobody. |
| `page_human` | a decision a human answer *could* unblock — a borderline gate score above all | **notify without ending anything**: SNS decision brief to the run owner, an `OwnerPaged` event, and a `HumanPaged` row in *this run's own* timeline. It does **not** yield the turn and does **not** pause the stage, so a page is always followed by `checkpoint` — which is the only call a directive can arrive in. Declared on the eval harness (r6) and on the conductor. |

Inside that contract, **which stage and task ran is the driver's fact, not the agent's.**
Outputs, metrics and evidence are the agent's to report — nobody else knows them. `stage`
and `task` are the opposite: the driver was handed both in its own invocation event, so the
agent's copy is a restatement at best. It used to be recorded anyway, and the first two live
monitor sweeps filed `"task": ""` because the agent simply omitted the field — leaving a row
that said a monitor stage completed without saying *which* of health/sweep/report did, which
is the ambiguity §2's sweep wiring exists to remove, reintroduced one layer down. Not
cosmetic either: the console derives which `(stage, task)` pairs a run executed from this
field, and an empty task matches **any** task of that stage, so a sweep could lend its
evidence to a health check that never ran. The dispatch now overwrites the echo rather than
filling it in when blank, because the dangerous case is not the omitted task but the
confidently wrong one.

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

**A named escape hatch must be one that can open.** Wiring `page_human` on the driver path
fixed half of that rejection; the other half named `launch_run`, which on a bus triage
*cannot succeed at all*. `service_launch_run` refuses without a KMS-verifiable approval
record, read from `args["approval"]` or from `params.approval_context` — and a triage built
by `triage_event_from_bus` has neither: nothing in the repo has ever written
`approval_context` (it was a read with no writer), and `approval` is not among the
properties `launch_run` declares in the orchestrator's harness, so the agent cannot supply
one either. The conductor was handed two doors, one of them painted on. Measured across
2026-08-05..08: of the 9 runs whose escalation was triaged, **4 produced no `HumanPaged`
event at all** — sent to a tool that refuses, they ran out of moves and the turn ended in
prose. The rejection now names only the exit that can work on *this* invocation:
`dispatch_is_possible(event)` decides, and when it is false the reason says plainly that
only a human can authorize a replacement run and that `page_human` is the one path that
changes anything. When a signed approval *is* present the dispatch advice is unchanged — a
guard that amputated the working case would hand the owner decisions the conductor was
authorized to make.

**On this path the conductor is not the first line to a human — it is the only one.** The
triage clause calls it "the FIRST line", which was true of the driver's own
`handle_escalate` (that publishes to the escalation SNS topic before it emits). It is not
true of the state machine's `EscalateFail`: that is a bare `events:putEvents`, and the bus
has exactly one rule (`llmops-escalation-triage`) with exactly one target (the driver).
There is no SNS anywhere on that path. So a triage that ends without resolving,
dispatching or paging tells **nobody** — the run row reads `failed`, the execution reads
FAILED, and the only trace is a log stream. That is what made this defect look
intermittent rather than total: **11 of the 11 directives ever parked were
`deliverable: false`**, and the runs that got no page were exactly the ones where the
conductor obeyed the rejection and tried `launch_run`. Live casualties include
`run-20260808T005301Z-c8b13faa`, `run-20260805T144522Z-86ab8a14` and
`run-20260808T024809Z-b56281da` — each an ARC-2 lineage run that died with its scientific
work complete and its owner never told. `_backstop_page` now closes it: a triage whose
outcome is not in `TRIAGE_ANSWERED` pages the owner on the way out, saying explicitly that
the page is the driver's backstop and not the conductor's judgment. It wraps the `return`
in `handler` rather than any branch inside the loop, so it covers every way a triage can
end without answering — prose after the re-asks, an unsupported tool, a rejected page, a
`stage_complete` that decided nothing — and the crash path too, where a bus triage has no
task token and `send_task_failure` therefore carries the news to no one. It is best-effort
by construction: a page that cannot be sent must not turn a merely-unanswered triage into a
crashed invocation, which would trade a silent failure for a louder wrong one.

**Who a record is about is decided by the invocation, never by the agent.** The backstop
answers *whether* a page happened, and that is not the same question as whether the owner
can find it. A triage runs under `run_id = triage-<subject>`, with the real subject passed
down as `params.escalation.run_id` — and nothing read that key. Every consumer took the
subject from the model's own tool arguments and fell back to `event["run_id"]`, so a
conductor that omitted `run_id`, or echoed back the id it was invoked under, addressed the
record to itself. Measured over every `HumanPaged` row on file (12, full scan), **3 are
filed under a `triage-` id** — `86ab8a14`, `c8b13faa`, `b56281da`, the same three ARC-2
runs above: the alert fired and the timeline the owner opens is empty. Requiring the
argument is not the cure; `run_id` is not in `page_human`'s `required` list at all and *is*
in `resolve_escalation`'s, and a model omitted it anyway — a schema's `required` is a
request to a language model, not an enforcement. One `triage_subject(event)` now serves all
three call sites, and the agent's copy is consulted only when the event carries no subject
at all (the console chat path, where `event["run_id"]` **is** the subject). The same
derivation had been spelled three ways, and `_backstop_page`'s copy was the correct one —
which is why the backstop's own pages are the properly filed ones, and why this looked
intermittent. A `resolve_escalation` naming no run is now **rejected** rather than skipped:
`if subject:` used to fall past `put_directive` and the reachability check to
`{"status": "resolved"}`, a status inside `TRIAGE_ANSWERED`, so the backstop stayed quiet
too — an unanswered escalation reported as answered.

**An emitted event with no rule is a promise with no path to it.** The paragraph above
says an `EscalatedToHuman` event "routes to the *driver*". It did not. The
`llmops-pipeline` bus carried **zero** EventBridge rules from Phase 1 through Phase 5,
while that detail-type was emitted from three places, documented here as routing to the
conductor, and serviced by a driver branch nothing could ever reach — `task="triage"` had
never once been dispatched. On a live bus this is the quietest possible failure: the
`PutEvents` succeeds, the event lands, and nothing happens. There is no error, no metric
and no log line, because "no rule" and "rule missing" are the same observation. The rule
now exists (`llmops-escalation-triage`, on the **custom** bus — the SageMaker rule beside
it uses the default bus because service events land there and cannot be moved, and copying
that shape here would produce a rule that is live, healthy in the console, and matches
nothing forever), and which detail-types *require* a listener is declared in
`EVENTS_NEEDING_A_RULE` in the contracts, so the decision is checked offline instead of
inferred from whichever rules happen to exist.

Wiring it up forced two emitters to be **renamed**, because the discrimination has to live
somewhere an EventBridge pattern can read it — a pattern cannot read prose:

- `_maybe_failover_model` overrides the model after a vendor 5xx burst and the retry
  *continues*. It announced itself as `EscalatedToHuman` with the words "informational,
  pipeline continuing" buried in a reason string, which was harmless only while nothing
  subscribed. The first rule routing that detail-type to triage would have paged the
  conductor about a run that had just healed itself. It is now `ModelFailedOver`.
- `handle_page_human` emitted `EscalatedToHuman` *too* — but a page is what the conductor
  emits when it has **already** triaged and found the decision above its authority, while
  `EscalatedToHuman` means "a conductor should look at this". Sharing one detail-type made
  the new rule feed itself: escalate → triage → page → triage, every lap a billed harness
  turn. It is now `OwnerPaged`, and the rule *also* excludes `stage: orchestrator` as a
  second line of defence. That exclusion uses `anything-but`, which does **not** match an
  event lacking the key at all — so `EscalateFail`, which carried only `run_id` and
  `iteration`, now carries `stage` too. Without it every terminal pipeline failure, the
  escalations that most need a triager, would have been dropped by the filter meant to
  protect them.

A triage also runs under its **own** synthetic `run_id` (`triage-<subject>`), not the
escalated run's. `take_directive` is keyed on `event["run_id"]` and the checkpoint branch
is its only caller, so a triage invoked under the subject's id would pop the subject's own
parked verdict — the one the conductor is in the middle of writing — and receive it as an
instruction from an accountable human. The conductor would be answering itself. The subject
arrives as `params.escalation.run_id`, which is what the prompt's triage clause already
reads, and its manifest is the subject's because a triage has none of its own. The envelope
is translated in Python at the driver's entry point rather than by an EventBridge
`InputTransformer`, for the same reason the rule exists at all: a transformer referencing a
path an event lacks drops it silently, and the two emitters of this detail-type carry
different key sets.

**That choice puts the channel's correctness in the deploy, so the deploy checks it.** A
later driver deploy — from a branch that predated this work — shipped a handler with no
`triage_event_from_bus` while `llmops-escalation-triage` stayed ENABLED and pointed at it.
Every escalation then reached the driver as a raw EventBridge envelope and died on
`KeyError: 'run_id'` before any handler branch ran. **None of the offline guards could see
it**, and that is the part worth understanding: they compare `EVENTS_NEEDING_A_RULE`
against the rules *this tree's* deployer builds, so a branch carrying neither the
declaration, nor the rule, nor the translator is perfectly self-consistent and green. A
tree cannot know which rules are live; only the bus knows. So `07_lambdas.py` now asks the
bus, at deploy time, before `update_function_code`: for every ENABLED rule targeting the
function being deployed, each `detail-type` it delivers must have a translator declared in
`BUS_DELIVERY_TRANSLATORS` and *defined* in the handler about to ship — or the rule's
target must carry an `InputTransformer`, since the two are alternatives. A gap is a
`SystemExit`, not a warning, for the same reason `config_subst.resolve()` raises: the
deploy reports success either way, so a warning is read by nobody. An unreachable bus is
reported as `unchecked` rather than clean — returning "no disagreement" for "I could not
look" would rebuild the exact ambiguity this whole section exists to remove.

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
skip the structured call), the driver re-asks up to `PROSE_REASK_BUDGET = 2` **consecutive**
times in the same session, then fails the stage — narration is never promoted to success.
Any serviced tool call re-arms the budget: what it counts is an agent that has stopped
speaking protocol, not one that slipped twice an hour apart and recovered. Every
fleet prompt also states this contract explicitly as a TURN-END INVARIANT naming that
harness's own terminal tools, with a write-first rule (artifacts land in S3 before the
call that claims them) — a guard test derives both directions from each harness's
declared tools, so the sentence cannot drift from the tool list. The budget for *filtered*
turns is a separate constant of the same value (`FILTERED_TURN_BUDGET = 2`): the numbers
agree today and the causes do not, and one shared constant would silently couple them.

**Evidence is spent as budget; it is never read as a verdict.** This is the most-cited
failure in the system's history — `MissingStageComplete` was the cause of death in 15 of
32 runs, fatal every time — so it is worth being explicit about the fix that was *not*
taken. When the prose budget runs out the driver re-derives a **claim** from durable state
(`_derive_stage_claim`: the S3 URIs named in the manifest's `stages[stage]` entry and in
the thin `"<stage>.<task>.i<n>"` entry agents write beside it), re-verifies it itself with
`verify_outputs` (per-URI `head_object`, zero new IAM), and fingerprints the result as
`(present_count, total_bytes)`. What that buys is **turns, not success**:

| Claim | Reading | Grant |
|---|---|---|
| non-empty, nothing missing, fingerprint **unchanged** | looks finished | one `EVIDENCE_REASK_BUDGET` turn, quoting the driver-verified URIs back and asking for `stage_complete` on them |
| fingerprint **strictly grown**, or partially present | still working | one `PROGRESS_REASK`, capped at `PROGRESS_REASK_CAP`, naming `checkpoint` (or `job_launched` if a job is running) |
| empty, or every object absent | no evidence | bit-for-bit today's `MissingStageComplete` |

If the grants are exhausted *with* verified evidence the stage settles under a new,
distinguishable name — **`UnclaimedStageOutputs`** — whose cause names the count and the
first two URIs the driver verified. The run still escalates; nothing auto-succeeds. What
changed is that a human now gets a list and can re-dispatch one stage instead of re-running
the pipeline. The reason it cannot be more than that: every piece of that evidence is
written by the agent being judged — the S3 objects and the manifest entries both — and a
role that can rewrite the artifacts it is verified against can launder its own evidence.
Trading 15 loud failures for an unknown number of quiet false successes is strictly worse,
because a false `SUCCESS` is written into `manifest["stages"]`, which every later stage
reads through `stage_fact_params`, and into the report a human approves. The claim is
therefore deliberately **not** written back to the manifest, and the driver's control-plane
evidence (`job_launched` parking a token, EventBridge resuming on real SageMaker job state)
stays the only channel the agent cannot forge. `UnclaimedStageOutputs` splits off
`MissingStageComplete` on the same argument that split `ModelUnavailable` and
`ContentFiltered`: the two need different operator responses. No ASL change is needed —
every state already catches `States.ALL` → `EscalateFail`, and a guard test asserts the new
name appears in no `ErrorEquals`, so nobody can quietly add "just retry it".

**Per-stage protocol records make that rate measurable.** `re_asks`, `filtered_turns` and
typed-call recoveries used to live only in memory and in `print`, and the sole per-turn
durable write is a heartbeat that overwrites itself — so a stage that burned three
malformed turns was **indistinguishable** from a clean one in every durable artifact, which
is why p̂ = 0.8421 had to be counted by hand out of logs. The driver now writes one row per
stage epoch into `llmops-stage-events` under `sk = protocol#<stage>#<task>#e<epoch>`
(zero new IAM, zero new resources) carrying `turns`, `serviced_turns`, `prose_turns`,
`filtered_turns_total`, `recovered_typed_calls`, `recovered_ending_in_prose`,
`infra_error_turns_total` and `deadline_cuts`. It is written **after** the stop-reason
override and the typed-call recovery, not before — that ordering is the defect in the
`[driver] turn` log line, which prints early and so reports `tool=None` for turns that were
in fact serviced. Absolute values, not `ADD`, with
`ConditionExpression="attribute_not_exists(turns) OR turns <= :turns"` so a late replay
cannot walk a counter backwards; a rejected condition is swallowed and printed, because
telemetry never kills work. The counters ride the heartbeat payload alongside `epoch`, so a
resurrected stage continues its numbering instead of overwriting it. Two placement traps,
both verified against the console's real code: `"protocol#" > "A"` = `TIMELINE_SK_MAX`, so
the rows fall outside the timeline query and the console needs no change (a guard test pins
that lexicographic fact); and `_recent_session_ids` queries the events table with **no sk
filter** and reads `json.loads(detail)["task"]`, so a row without a `detail` attribute would
fabricate a session id with no spans — hence `detail` always carries `task`, pinned in both
directions. `protocol_rollup(items)` is a pure function over exactly those rows and yields
the structured-call rate per stage and per run.

Two read-only tools close the loop. `tools/audit_drift.py` answers "is production running
this tree's code?" across five legs (IAM inline policies, the state-machine definition,
harness configs, Lambda configuration, Lambda code per zip member) using the deploy
scripts' own comparators, and exits `0` only when every leg was both compared and clean —
`2` means something could not be looked at, which is not a pass. It exists because the
answer was measurably *no* for weeks: before the 2026-08-15 rehearsal, `eval_only` had never
been deployed, `_check_mode_prerequisites` was absent from the live start Lambda, and 3 of 7
prompts plus 4 of 7 Lambdas had drifted — none of it visible to a test suite that reads the
working tree. `tools/probe_protocol_reliability.py` derives the sample size rather than
hard-coding it: to say with 95% confidence that the true pass rate is ≥ p after n
consecutive passes needs `p**n <= alpha`, i.e. `n = ceil(log(alpha)/log(p))` → **14 runs
($7.42)** for 0.80 and **29 ($15.37)** for 0.90 at `PROBE_UNIT_COST_USD = 0.53` (the
measured cost of the 2026-08-15 rehearsal). Its Wilson interval is pinned to the console's
`_wilson` to 1e-12 by test, and its ledger is written **before** each dispatch, so a slot is
never dispatched twice across days. The 42% five-stage and 13% twelve-stage predictions are
**not** retired by any of this — they are re-measured by running the probe, which is
authorized separately.

The turn handoff itself is guarded by a **heartbeat + resurrector** pair. The driver's
self-reinvoke is an async Lambda invoke — fire-and-forget, and Lambda dropped one live
(run 68cfa9c8 sat dead nine hours with its token parked and Step Functions still
RUNNING). The driver now stamps `driver_beat_at` and the exact re-invoke payload on the
run row before every turn; a scheduled `llmops-resurrector` (every 15 min) re-invokes
the driver for any running run whose beat is stale with **no parked task token** — a
parked token means launch-and-release is waiting on a SageMaker job by design, and that
wake belongs to resume_pipeline. The claim is conditional on the beat the sweep read
(no double-resurrection), capped per run (past the cap it escalates: a driver that dies
every turn has a defect revival only re-runs).

Triages get the same protection through a different door (#37): they deliberately have
no run row, so the refused runs-table heartbeat routes into the events table's dedicated
`__liveness__` partition instead — payload including `params`, because a triage's work
order exists nowhere else. The resurrector reads that one partition with a Query and
applies the same stale/cap/claim contract; a terminal return **deletes** the item (an
ending is not a death, and a delete leaves no immortal history and no inherited
resurrection count), while a cap-exhausted item escalates against the run the triage was
*about* — never its own `triage-` id — and is deleted with the escalation. Scheduled
jobs (sweep, finops) are deliberately **not** revivable: a crashed schedule waits for
its next schedule rather than re-running non-idempotent work.

That whole loop is now **live-verified against the deployed bundles**, not just unit-tested: `tools/probe_liveness_resurrection.py` downloads what Lambda is serving, drives a synthetic triage through beat → sweep → claim → revive → cap-escalate → terminal delete against the real events table, and passed **18/18** on 2026-08-12. It exists because this is the one path whose healthy state and broken state read identically — a triage dies about once a month, so `checked_liveness: 0` is normal on almost every day, and a Query on the wrong partition, a missing `dynamodb:Query`, or two separately-bundled copies disagreeing about the string `__liveness__` all produce that same reading. See TEST_RESULTS for the six mutants it kills.

Both halves end by **printing** what they checked, because the schedule's invoke is
asynchronous and the counts the handler returns are read by nobody. That matters most
for the non-run half, whose healthy state is *zero* beats on any day no triage is dead:
without the line, a sweep working perfectly and a Query aimed at the wrong partition
left identical traces — start, end, silence. The printed counts are the standing evidence
that the partition is read at all.

Printing is not noticing, though, and for a long time nothing did. Between 2026-07-29 and
2026-08-12 Lambda **dropped 19 async invocations** (driver 11, resume 8) — each one a
stage that stopped with its token parked — and there was not one CloudWatch alarm on any
function in this system: the nine-hour death was found by a human reading an execution
history, the resume Lambda's missing `events:PutEvents` grant by a human reading logs a
day later. `deploy/06_observability.py --alarms` now creates thirteen, in three families
that each detect something the others cannot. **`<fn>-errors`** (every function *any*
deploy script creates, derived from each of them so a new Lambda cannot ship unwatched)
is the primary detector: every one of those 19 drops landed on a day that also had function
errors, and resume's ratio is exactly 3 — one attempt plus the two default retries — so
this family fires first, always. Derived from *each* deploy script rather than one, because
one was measurably not enough: the census read only `07_lambdas.py`, and the eighth
function this account runs — `llmops-admin`, the console API every plan signature and every
human verdict goes through, 17,007 invocations in three days — was created by
`deploy/console/deploy.sh` and therefore had no alarm of any family until 2026-08-12.
**`<fn>-silent`** covers the three scheduled functions
whose *silence* is the failure; `TreatMissingData` must be `breaching` there, because an
uninvoked Lambda publishes no datapoint rather than a zero, and with the ordinary
`notBreaching` the alarm would sit in INSUFFICIENT_DATA forever and detect precisely
nothing. `llmops-start-pipeline` deliberately has none: its nightly schedule ships
disabled, and a permanently-red alarm teaches an operator to ignore the whole set.
**`<fn>-async-dropped`** is kept not as an earlier warning but as a different *meaning* —
an error that retried is self-healed, while a drop is work that is gone. There is no
`ExecutionsFailed` alarm on the state machine: a run that honestly fails its quality gate
is this pipeline working, not an incident.

Logs had the mirror-image problem: a retention policy that existed and was pointed at the
wrong surface. `setup_observability.py` has capped its delivery log group at 30 days since
the deliveries were first wired — but measured over the seven days to 2026-08-12, those
groups received **0 bytes**, while **1225 of the 1236 MB** this system ingested landed in
`/aws/bedrock-agentcore/runtimes/<id>-DEFAULT` groups and `/aws/lambda/llmops-*` groups that
**never expire**. Both are created by AWS, not by this repo, which is why nothing here had
ever set a policy on them. `--retention` applies the same 30 days to every group this system
fills, from two sources for two creators: the Lambda groups are **repo-derived** (the same
census the alarms use, so a function cannot be watched-but-unbounded), and the AgentCore
groups are **discovered from the account**, because their names carry harness and runtime ids
this repo never sees — building them from `HARNESSES` instead produces five empty groups
beside the ones holding the traffic, which was tried, measured and discarded. Foreign log
groups sharing the prefix are left alone: this account is shared, and another project's
retention is not ours to set.

The last exposure that pair does *not* cover is the session's own clock. AgentCore
reclaims a runtime session at `maxLifetime` = **28800 s (8 h)** — a hard cap that
activity does not reset and no setting raises. A distillation stage runs 8–12 h in one
deterministic session, so it outlives it, and the invoke that crosses the line fails as
an ordinary runtime error: the driver would spend its stream-salvage retry, then its
re-ask budget, on a session that can never answer again. So the driver rolls **before**
the cap instead. Each stage carries a **session epoch** in its continuation payload;
between turns (never mid-turn, where an unanswered `toolUse` is outstanding) past
`SESSION_ROLLOVER_S` = 25200 s it increments the epoch, opens `…-e<N>`, and re-seeds it
with the task payload plus a resume instruction — the pending message cannot travel,
because it is usually a `toolResult` answering a `toolUse` the new session never issued.
Nothing is lost: every stage's state is in S3, which is exactly why the 2026-08-08 hand
resurrection worked. The epoch is *carried*, never derived from a clock, so the
self-reinvoke and the resurrector always rebuild the same session id rather than two live
sessions sharing one task token; the 3600 s of margin covers an 840 s turn already in
flight plus the continuation behind it. Rolled ids are appended to the run row because
they are the one thing the console's `(run, stage, task)` reconstruction cannot derive,
and unscored spans on the longest stages is a silent loss of exactly the interesting data.

## 4. Launch-and-release with EventBridge wake

A harness turn is bounded (~14 min); a training job runs for hours. The rule: **a harness
never waits on a job**. The finetune agent launches the SageMaker training job — and the
eval agent its student-inference job, which rides the same training-job rail — calls
`job_launched`, and its session is released. A tracked job that ends **Stopped with $0
billed** is capacity, not code — a race loser or an abandoned quota wait never ran and
proved nothing — so the resume Lambda settles the token as `CapacityStopped` and the
launch state re-enters itself with the remediation iteration unspent, up to 3 free
relaunches per run; the 4th counts as `TrainingJobFailed` like any real failure. (Eval earned this path the hard way: with no
`job_launched`, its only way to span a long inference job was polling in-turn, which is
where prose turn-ends happen; the machine's `EvalScore` state now picks up scoring in a
fresh session after the job completes.) The chain that resumes the pipeline:

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

**A dead token is an answer, not an error — on both Lambdas.** The reading above was
resume-only for ten days, and the driver paid for it four times: `TaskTimedOut` out of the
re-asks-exhausted settle, re-raised by the `handler()` wrapper, so Lambda marked the async
invocation failed and retried it **twice** (2026-08-09 at 05:50:48Z, 05:52:03Z, 05:54:28Z;
once before at 2026-08-05T15:39:51Z). Each retry was a fresh **billed** AgentCore turn
re-running an agent whose stage had already been decided, against a token none of them could
settle. All four of the driver's settles now go through one `settle_token()` funnel, and
what "gone" means lives in `pipeline/contracts/task_tokens.py` — imported by both Lambdas,
defined by neither, because two constants in two files agree only until someone edits one.
The discrimination is unchanged and is the whole point: a throttle or 5xx still raises, so
the settle can be retried rather than stranding the token for its full `TimeoutSeconds`.

**Describing the work must not be able to cancel it.** Both settle paths above assume the
settle is *reached*. On 2026-08-11 at 04:22 UTC one was not: `llmops-resume-pipeline` had
shipped without `events:PutEvents`, so `emit_event(ModelTrained)` — which sat immediately
*before* `send_task_success`, inside the same `try` — raised `AccessDeniedException`, and a
successfully-finished training job's stage never learned it had finished. EventBridge
retried into the same wall twice and the third delivery became an `AsyncEventsDropped`: 6
errors, 2 drops, `run-20260811T040003Z-3548116f` left with a parked token for the
resurrector to find. The grant is fixed, but a grant is not the lesson — **the ordering
that let a missing grant do that is.** The driver has had the opposite rule since #52
(`test_a_failed_bus_emit_still_settles_the_task_token`); resume's three emits now go
through the same wrapper, and a source-derived test fails on a fourth emit written the old
way, so the rule cannot be forgotten one call site at a time. The trade is explicit and
cheap here: the bus carries exactly one rule (`EscalatedToHuman`, which this Lambda never
emits), has no archive, and the stage-events table is written by the driver directly — so
nothing consumes these three events today, while the settle releases a paid-for stage. A
skipped emit prints, and `llmops-resume-pipeline-errors` (§ alarms) no longer needs that
print to be a traceback in order to notice.

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
`actorId` partitions namespaces while retrieval can still cross-read shared facts.
USER_PREFERENCE/SUMMARIZATION strategies are deliberately skipped — there is no human user
in the loop.

This is the mechanism by which run-N learnings reach run-N+1, and it is proven: in
Phase 5's e2e run, the finetune agent launched its training job **first-try** using the
floors-only-requirements + torch-2.6-DLC recipe learned in Phase 3's remediation gauntlet
— a recipe that had cost 5 failed jobs to discover.

Two properties of the wiring are load-bearing, and both were learned by measuring the live
fleet rather than reading this repo (the measurements are in
[TEST_RESULTS.md](TEST_RESULTS.md), finding D14):

- **The harness list is derived, never written down.** It reads `harnessName` out of every
  `agents/*/harness.json` — the same producer `deploy/05_harnesses.py` deploys from — so a
  new agent is wired by existing. A hand-written list of the five pipeline workers is why
  `llmops_finops` and `llmops_orchestrator` never received the semantic-retrieval tightening
  and sat at the pre-fix `topK` 10 / `relevanceScore` 0.2 for as long as it had been fixed
  everywhere else. Wiring zero harnesses, or one whose config has no `harnessName`, refuses
  rather than reporting a clean deploy.
- **An `actorId` already live is never changed by a redeploy.** `actorId` is the partition
  key of `/users/{actorId}/facts`, so rewriting it does not move a memory — it abandons one,
  and `UpdateHarness` returns success either way. The two harnesses above are wired under
  their full harness IDs by the older `deploy/wire_memory.py`, and those partitions hold 43
  of the memory's records. Moving one is therefore opt-in per harness
  (`--repartition <harness>`) and refuses unless the data plane can report the record count
  it is about to abandon: an unknown count reads exactly like a count of zero.
- **Every attach reports what the other spelling of `actorId` still holds.** The guard above
  protects a partition from *this* deploy and does nothing about one an earlier deploy already
  walked away from — live, that is 63 further semantic records (and 105 episodic) sitting under
  the five pipeline workers' full harness IDs, unreachable by the agents that wrote them since
  a redeploy moved them to the bare name. No API moves a record between namespaces, so this is
  a report rather than a repair; what it buys is that an abandoned partition and an empty one
  stop printing the same. Both candidate spellings are checked, both channels are counted, and
  a partition that could not be read is reported as unread rather than as empty.
- **And once per run, every actor on the memory that no harness points at.** The check above
  compares the two spellings *this repo produces*; `ListActors` returns 16 actors on the live
  memory and two are neither — `monitor` (3 semantic records) and `monitor-agent` (6), written
  by the older `deploy/wire_memory.py`, whose `--actor-id` is free-form. A candidate list
  derived from a repo's naming conventions cannot contain a spelling the repo never had, so the
  memory-level sweep is derived from the data plane's own enumeration: live, **9 orphaned
  actors holding 72 semantic + 108 episodic records**. The two checks answer different
  questions — which harness lost a partition, versus whether anything on the memory is orphaned
  at all — so their counts overlap and are not additive. The reachable set is read for every
  harness the repo defines, not only the ones a given run wires, or `--harness <one>` would
  denounce the other six healthy partitions.

The semantic channel is deliberately **tighter** than the episodic one (`topK` 5 /
`relevanceScore` 0.6 versus 10 / 0.2). They are not inconsistent: `/episodes/{actorId}/{sessionId}`
scopes episodic recall to the agent's own session, while `/users/{actorId}/facts` is the
cross-**run** channel, where a loose threshold delivers another run's conclusions as truth.
Every prompt that receives retrieved memory also carries a precedence rule subordinating it
to this run's signed plan and this run's own measurements.

## 7. Fail-closed gates

Quality gates default to FAIL. This was made strict by a live defect (Phase 5, e2e
iteration 4): a mini-run's eval agent emitted `gate_passed: null` + `needs_human: true`,
and the old default-True coercion *promoted a null to a pass*. The fix in the driver:

- On an eval gate task, `gate_passed` is `metrics.get("gate_passed") is True` — absent,
  null, or anything non-True means NOT passed. Regression-tested.
- The gate held in Phase 4 the way that matters most: the model genuinely failed
  (0/16 twice, `closed_think_rate` 0%) and the verdict **stood** — it was not "fixed"
  into a pass. A gate you can talk your way past is not a gate.

**`escalate_human` ends the run; `checkpoint` is the pause.** Five harness tool descriptions
used to introduce `escalate_human` with *"The pipeline pauses"* — the exact opposite of what
the driver does, and the TURN-END INVARIANT compounded it by naming `escalate_human` as the
call "when blocked". An agent blocked on a decision a human could make would therefore reach
for the one call that guarantees the human's answer arrives too late: `_mark_run_escalated`
sets the run `escalated` and `send_task_failure(error="EscalatedToHuman")` fails the state
machine task, and because `escalated` is an unreachable run state, `put_directive` returns
`reachable: False` and the console tells the operator their verdict "CHANGES NOTHING".
`checkpoint` is what they wanted: it yields the turn, keeps the run alive, and is the channel
a directive is delivered on. Escalation is for the case where no human answer could let the
stage continue.

**A blocked run could be answered, or noticed — never both.** `checkpoint` keeps the run
answerable and notifies nobody; `escalate_human` notifies and makes the run unanswerable.
So the eval gate's third CI outcome — *borderline*, the one gate verdict a human answer can
actually settle — was routed to `escalate_human`, which destroys the run the answer was for:
the same undeliverable-verdict shape as the three-day data-prep escalation above, arrived at
from the prompt instead of from the code. And the state that keeps a run answerable wrote
*nothing anywhere*: a stage paging and then checkpointing until an answer arrived was
byte-identical to a stage doing its work — same run row, same events, same execution status —
so **the one state whose entire purpose is to get a human's attention was the one state the
console could not show** (the third time that lesson has been paid for: §7's escalation
wording and §5's parked-verdict rendering were the first two).

`page_human` is the third channel, and the half that is easy to get wrong is the turn. A
triage page IS terminal for the turn — deciding whether to page is a triage's whole job — but
a *stage* page must not be: the invocation is holding a Step Functions task token, `_ack_terminal`
does not settle it, and returning would leave `EvalGate` waiting `TimeoutSeconds: 86400` on a
token nothing is left alive to settle. An agent that did exactly what the new prompt asks
would hang its run for a day. The discriminator is `event.get("task_token")` — a property of
the invocation, not of the caller's identity: `triage_event_from_bus` provably omits the key,
because the state machine already failed the token it had.

**Waiting is derived, never stored.** The console answers "is this run blocked on me right
now?" from rows two independent writers already produce: the `HumanPaged` row, the
`WaitingOnHuman` row the driver files on each checkpoint that follows it, and the parked
`directive#` rows. A directive at or after the page ends the wait *at the moment it is
parked* — delivery happens inside a live invocation the console cannot see, and asking again
for a decision somebody already made is the false alarm that teaches an operator to ignore
the true one. Two bounds matter: the driver stops writing waiting rows at `WAIT_ROW_CAP = 12`
(twice the prompt's 6-checkpoint cap, since a prompt is a request and `maxIterations` is 100),
so the console reads each row's own `waiting_turn` field — a floor — and renders `12+` rather
than counting rows, which past the cap is not even a floor; and the derivation runs its own
**reverse** query rather than reusing `_timeline`, because a paged run is by definition read at
its newest end and the derivation must not inherit whichever end a shared reader happens to
prefer — a coupling that was a live defect when this was written and is now merely avoided
(§13).

Scoped deliberately: `data_prep` and `finetune` keep `{checkpoint, escalate_human}` until
each gets protocol wording of its own. A tool declared without a protocol naming when to
reach for it is a tool an agent reaches for at random.

### 7b. Retrieval-aware runs (RAFT, r6d): the facts move out of the weights

r6c measured the structural dead end behind every honest gate FAIL to date: correct
decontamination deletes exactly the org-specific facts the acceptance set demands (41% of
rows), and no student size fixes facts absent from training data
(`deploy/evidence/SCALING_DIAGNOSIS_r6c_8B.md`). The exit with production evidence
(`deploy/evidence/RESEARCH_r6_direction.md`) is RAFT-style retrieval: **the facts live in a
Bedrock Knowledge Base** (`deploy/09_retrieval.py`, per-run, human-deployed, torn down after
eval) **and the training rows stay decontaminated.**

The wiring is three activation-gated prompt clauses plus three plan params
(`retrieval_kb_id`, `retrieval_k`, `retrieval_distractors` — all plan-classified; a
closed-book plan omits them and runs the classic path untouched):

- **data-prep `curate`**: probes the KB with 3 corpus tickets (zero hits = escalate, never
  context-free rows that LOOK like a RAFT corpus), then assembles each surviving row's user
  turn per the canonical format at `code/eval/raft_context_format.md`, recording
  `raft_format_sha256` in `stats.json`. **Decontamination stays on the bare ticket text** —
  context passages MAY resemble acceptance questions by design; the acceptance *files* are
  structurally excluded from the index at ingest (inclusion-prefix refusal + the
  Retrieve-only IAM fence).
- **eval `evaluate`**: the student answers open-book — retrieve per acceptance item, same
  canonical key, digest into `report.json` (equal digests in `stats.json` and `report.json`
  IS the train/inference format check), per-item evidence in
  `evaluation/retrieval_details.jsonl`; a failed retrieve is an empty context block, counted
  and scored, never an unscorable.
- **eval `score`**: **the judge stays blind to retrieval** — bare ticket, answers alone.
  The 0.45 bar predates retrieval and must go on measuring the same thing, or r6d's number
  cannot be compared to r6c's 0.223.

## 8. The remediation loop (≤ 3 iterations)

`QualityGateChoice` fail → `RemediationChoice`: while `iteration < 3`,
`IncrementIteration` → `RemediateFinetune` → back to `FinetuneAnalyze` →
`EvalGenerate` → `EvalGate`. The loop rejoins **above** the generator, not between it
and the gate: rejoining below would have iteration 2 re-gate iteration 1's report, and a
remediation that changed nothing could "pass".
Budget exhausted → `EscalateFail`. The same budget appears inside the agents' task
prompts ("diagnose, fix, retry — max 3; then `escalate_human`"), so the machine-level
and agent-level budgets agree.

A **second, disjoint loop** covers a failed eval *inference job* (found live on
run-20260811T040003Z-3548116f): `EvalGenerate` catches `TrainingJobFailed` →
`RemediationChoiceEval` (same `iteration < 3` budget) → `IncrementIterationEval` →
back into `EvalGenerate` itself. It deliberately does **not** join the finetune loop:
that loop re-trains, and an inference-script defect — the live case was an SDK-encoded
hyperparameter read raw — is not curable by retraining. The eval agent re-enters, reads
its own failed job's `FailureReason`, fixes its own code, and relaunches.

The loop's honest edge case is its best evidence (Phase 5, run 5): the eval agent
returned `FAIL_CLOSED_NO_INPUT` (no quality signal exists at 2-sample scale), the machine
correctly armed `RemediateFinetune` — and the finetune agent answered
`REMEDIATE_PREMISE_INVALID — no quality signal to remediate` and escalated instead of
burning iterations on an unfixable premise. Honest-over-busy is a design requirement,
and the loop's exit for it is `escalate_human`.

## 9. Model failover is a design layer, not an emergency measure

Live-established (Phase 5): vendor model quotas are a HARD constraint — even AWS-internal
accounts are rate-limited by the model provider, and a multi-agent platform is its own
token-flood generator (6 harnesses then, 7 since `llmops_finops`, × agent loops × long
streams). ~12 Fable 5 5xx bursts
recurred across a single day. Design rules (full text in [AGENTS.md](../AGENTS.md)):

1. Every harness has a fallback chain, and it is now **two rungs**:
   `global.anthropic.claude-fable-5` → `us.anthropic.claude-fable-5` →
   `global.anthropic.claude-opus-5`. The first rung changes only the **inference profile**,
   not the model: same weights, same price, zero prompt changes, and a different Region pool
   (both are ACTIVE `SYSTEM_DEFINED` profiles, verified via `ListInferenceProfiles`). Only
   the second rung changes model family. The rung order follows the r6e measurement below —
   the 5xx burst was confined to one profile while a sibling profile of the same model took
   **zero** invocations in the same hour — so a model change should be the last resort, not
   the first: it is the rung that alters what the prompts were written against. Switched via
   `InvokeHarness`'s per-invocation `model` override — scoped to the one call, no
   control-plane write, nothing to restore. It was `UpdateHarness` until r6e's follow-up:
   that mints an immutable new version and repoints the DEFAULT endpoint, which is the
   endpoint every stage here invokes through (no `qualifier` appears anywhere in
   `state_machine.asl.json`), so one session's fallback moved the model under every other
   run in flight and then needed a ~15 s swap-plus-restore pair, on the control plane
   that was 5xxing at the time.
2. The "switch" signature: repeated `InternalServerException`/`ServiceUnavailableException`
   from ConverseStream while a direct single-shot probe of the same model succeeds —
   quota pressure, not an outage (it never surfaces as an explicit ThrottlingException).
3. Mixed allocation is the **designed** way to spread quota pressure — premium tier for
   judgment-heavy agents (orchestrator, eval), the fallback tier for process-execution
   agents (data-prep, deploy, monitor) — and it is **not what is deployed**. All 7 live
   harnesses run `global.anthropic.claude-fable-5`, verified against `GetHarness` and
   against `agents/*/harness.json`, which agree. So the mixed configuration is a lever
   the failover chain makes available, not a state the platform is currently in; the
   whole fleet shares one model's quota today. Previous versions of this list read as
   though the split had shipped, which is the same mistake as the diagram claiming
   VPC-isolated harnesses (§11) — an intended design read back as delivered fact.
   `tests/test_docs_claims.py` now asserts the model claim against the real configs.
4. The driver detects model-5xx during stream salvage and overrides the model for the
   rest of the invocation, emitting an informational failover event
   (`_maybe_failover_model` in `orchestration/harness_driver/handler.py`). Its one
   remaining control-plane call is a `GetHarness` read, to learn which model the harness
   declares and therefore which link of the chain to take; the driver role's grant is
   scoped to exactly that (`FailoverHarnessModel` in `deploy/iam/lambda_roles.json`). That
   grant was missing until r6e, so the read raised `AccessDeniedException` on every 5xx
   the failover was ever called for and the `except` that protects the retry path
   swallowed it — the mechanism had never once run in production, and said nothing.
   Full automated-failover hardening is Phase 6.
5. **This is an app-layer availability-degradation decision, and it is filed as one.**
   Measured over the r6e storm window in CloudWatch `AWS/Bedrock`:
   `EstimatedTPMQuotaUsage` peaked at **44,719 against a 4,000,000 quota — 1.1%**,
   `InvocationThrottles` returned **no data at all**, and `InvocationServerErrors` was
   **46 of 59 invocations, 78%**, entirely on one inference profile while a sibling profile
   of the same model and a different model family both took zero traffic. So these 503s were
   **not** quota-driven: a Service Quotas increase buys nothing here, and neither does the
   documented remedy for throttling — shedding your own load — because that remedy assumes
   your own demand is the cause. Switching model is likewise **not** a documented AWS remedy
   for `ServiceUnavailableException`; the documented ones are retry with backoff, and moving
   Region or using cross-Region inference, which is exactly what rung 1 now does. Hence the
   naming discipline: `_is_model_5xx` deliberately **excludes** `ThrottlingException`, and
   `_maybe_failover_model` is not hung off a throttling-named path, because the next reader
   who finds it there will go open a quota ticket for a capacity problem.
   The counterpart caveat: because the failover had never executed, **the health of any
   fallback rung has never been observed** — the chain is an untested hypothesis until a run
   exercises it.

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

### The only deadline that actually bounds a stage

Three timeouts are stacked here and only one of them is a ceiling on the *work*:

| Layer | Value | What it bounds |
|---|---|---|
| Harness turn budget | 840 s | one agent turn |
| Driver Lambda `Timeout` | 900 s | one **invocation** — not the stage, because the driver self-reinvokes via `_continuation` |
| State machine `TimeoutSeconds` | **86400 s** on long-work states, 3600–7200 s on the rest | the **stage**: how long the `.waitForTaskToken` token stays alive |

Because the driver hands itself the conversation across invocations, the Lambda's 900 s is
not a limit on how long a stage may run — the **task token's** lifetime is, and that is
`TimeoutSeconds`. The six states that wait on real agent work (`DataPrepGenerate`,
`DataPrepCurate`, `FinetuneLaunch`, `EvalGenerate`, `EvalGate`, `RemediateFinetune`) carry
**86400 s — a full day**, raised from 7200/21600 on 2026-08-03 on the platform owner's
instruction after a 480-teacher-call generation run was cut off at 7200 s mid-work.

The seven bookkeeping states keep an hour or two on purpose. `Teardown` is what deletes the
endpoint, and `MonitorHealth`/`MonitorReport` sit on the only path to it: a wedged
`Teardown` at 86400 s would hold an `ml.g5.2xlarge` InService for a day at $1.515/hr, which
is the precise shape of the 843-day, 0-invocation orphan this project already paid for. The
split is asserted by
`tests/test_orchestration.py::TestStateMachine::test_a_stage_that_deletes_the_endpoint_keeps_a_short_timeout`,
which also **fails on an unclassified new state** rather than defaulting it into either
bucket.

`FinetuneLaunch` and `RemediateFinetune` also carried `HeartbeatSeconds: 18000` until the
same change. That field is a liveness signal only if something sends heartbeats — and
nothing in this platform ever called `SendTaskHeartbeat`, though the IAM role grants it. So
the first heartbeat never arrived and both states really died at **18000 s while their ASL
said 21600**, with the console's hover card rendering a reassuring "heartbeat 18000s" row.
Both fields were removed, and
`test_a_heartbeat_interval_requires_something_to_send_heartbeats` now refuses the field
without a sender: a heartbeat interval with nobody sending is not monitoring, it is a
shorter deadline that no surface reports.

**Writing the sender is not the missing piece, and `HeartbeatSeconds` is not coming back.**
That test lets the field in as soon as something calls `SendTaskHeartbeat`, which reads as a
to-do; it is not one, and `test_no_harness_state_carries_heartbeat_seconds` now says so
unconditionally for every `.waitForTaskToken` state. The reason is that the platform already
has an authority to declare a stage dead — the driver's `__liveness__` beat plus the
`llmops-resurrector` (§7) — and the two cannot coexist. A heartbeat timeout makes Step
Functions fail **the state**, which mints a **new random task token** on the retry and
**does not terminate the harness that stopped beating**: only `.sync` integrations have
best-effort cancellation, the callback pattern promises nothing. So the orphan keeps
streaming, keeps burning the account-level TPM the whole fleet shares (§9: the r6e storm was
78% server errors on one inference profile), and keeps writing its own liveness beat. The
resurrector instead resumes the **same session on the same token** from the parked payload,
which preserves the partial work of a stage that is neither cheap nor idempotent — S3
manifest append and a wall-clock DynamoDB sort key, the same non-idempotence that is why
these states carry `Retry: NONE`. Two authorities to declare a stage dead means two
independent dispatches of its replacement, so exactly one may hold that authority, and it is
the one that resumes rather than restarts. The rationale is also written into the state
machine's own top-level `Comment`, because an absence with no explanation beside it reads as
an oversight and the guard protecting it reads as pedantry.

## 11. VPC posture for production

Harness configs (`agents/*/harness.json`) run PUBLIC-network for iteration speed. The
free substrate — VPC, two private subnets with no IGW and no NAT, both security groups,
the S3 and DynamoDB gateway endpoints — is built by `deploy/02_network.py`.

This paragraph used to end "and the Lambdas can run **VPC-isolated with interface
endpoints — no internet egress**". `deploy/07_lambdas.py` contains the string `VpcConfig`
**zero times**, so that was a capability with no deploy path — the same failure mode as
§9 item 3's model split, a design read back as a delivered feature. **Nothing in this repo
routes through an interface endpoint today**: `agents/*/harness.prod.json` does not exist
(let alone one with a non-`PUBLIC` `networkMode`), and `/llmops/network/*` is written by
`02_network.py:201` and read by nothing.

Which is why the 11 interface endpoints are the one thing that script now **skips by
default**. They are also the only part of it that bills, and it used to print
`0.01 × 11 × 24 = ~$2.64/day` while attaching every endpoint to *both* subnets — AWS
bills an interface endpoint "for each hour that your VPC endpoint remains provisioned in
each Availability Zone", because `SubnetIds` creates one endpoint network interface per
subnet and the ENI is the billed unit. The real figure is **$5.28/day**, and $2.64 was
the one-AZ answer. `endpoint_cost_per_day(len(INTERFACE_SERVICES), len(subnet_ids))`
derives it from both lists so a twelfth service or a third AZ cannot silently make the
printed number wrong again; `find_endpoint_consumers` reads the same files a deploy reads,
so it goes green on its own the day someone writes a VPC-mode harness, and
`--force-unused-endpoints` overrides it for anyone deliberately paying ahead of need.

The skill sources have moved: all **19 skill sources across the 7 harnesses are `s3`
sources today; none are `git`** — verified by
`tests/test_docs_claims.py::test_the_skill_source_claims_match_the_harness_configs`,
which reads the configs rather than trusting this paragraph, and which also rejects a
*mixed* state, because a half-done migration leaves some harnesses pinned and others still
floating on the skill repo's main. The mirror they read is built by `ensure_skills` in
`deploy/03_storage.py`, which derives what to mirror from the harness configs, validates
each `SKILL.md`'s frontmatter before uploading, and reads every one back, with the harness
role granted `GetObject` + `ListBucket` on `skills/*` and no write.

Each URI is written `s3://<DATA_BUCKET>/skills/...` and resolved at deploy time by
`deploy/config_subst.py`, because the bucket name embeds the account id and these configs
are public-repo files. That resolution is a hard failure, not a warning: an unresolved
token in a skill URI is *accepted* by `UpdateHarness`, mints a version and reports READY,
and only then fails at every session start — so `resolve()` raises rather than send it.

**Still not built:** the VPC-mode harness variants. Earlier revisions of this file
described `agents/*/harness.prod.json` and `deploy/05_mirror_skills.py` as existing; both
paths do not exist, and never have in any branch, so that was a design being read as a
shipped feature.

Two forcing functions made the mirror a prerequisite for VPC mode rather than an
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

`llmops_monitor`'s `health` and `report` tasks run *inside* the state machine: per-run, within a
run's lifetime, answering "is the endpoint alive now". Reconciliation is the opposite shape on all
three axes — it runs **after** the run is over (Cost Explorer lags ~24 h), it spans **many** runs,
and it answers to the project rather than to any one run. A run that finished yesterday has no live
agent to attribute today's settled bill, so putting this in `monitor` would mean a per-run agent
reaching across other runs' data.

The same three axes put monitor's own `sweep` task on a schedule rather than in the spine, which
is the clearest proof this is a shape argument and not a per-harness one: an orphaned endpoint
belongs to a run that has already ended — often one that *crashed*, and so never reached any state
that could have looked. Two tasks of one harness, on opposite sides of the boundary, each placed by
what its question is about.

So it sits beside `llmops_orchestrator`, above the spine: **the conductor decides what to spend,
the auditor reports what was spent.**

Its IAM is read-only on billing (`ce:Get*`, `pricing:*`, `budgets:ViewBudget`) and it has no
authority to terminate anything. Two reasons, and the second is the load-bearing one:

- An auditor must not be able to change what it audits.
- Stopping a run is the orchestrator's authority via `page_human`. Giving kill rights to the
  component whose job is to *observe* puts spend-control authority in the wrong place — and an
  auditor that can act on its own findings has no independent check on those findings.

The $20,000 approval reference lives in the console and `cost_model.py`, not in the auditor, for the
same reason: the thing that measures spend is not the thing that authorises it.
See [COST.md](COST.md).

Least-privilege IAM throughout (no `*FullAccess`), all resources scoped to `llmops-*`,
and this being a public repo: no account IDs anywhere — deploy scripts substitute
`<ACCOUNT_ID>` at run time, enforced by a pre-commit hook and CI redaction scan.

## 13. finetune-vision — the first non-LLM task type, and what "generic" bought

The vision line (`pipeline_mode: "vision"`, catalog key `finetune-vision`) fine-tunes an
Apache-2.0 detector (RT-DETR by default, D-FINE vendored) on the customer's own
COCO-format labeled images and judges it with a deterministic mAP gate. Architecturally
it is a demonstration that the platform's genericity was real: **the driver, the IAM
policies and the Lambdas are byte-identical to the LLM lines**, and the state machine
gained exactly one Choice rule whose `Next` equals the Default — an explicit route to the
same full path, added because the mode-drift guard demands every mode with declared
prerequisites be visible in the machine.

What a mode actually is here: (a) a routed string; (b) entries in three dispatch tables —
`MODE_REQUIRED_PARAMS` (presence), `MODE_REQUIRED_ROLES` (was the model *chosen*, since
`DEFAULT_MODELS` would silently hand a detection run a language model), and
`MODE_REQUIRED_CHOSEN` (was the *value* chosen, since `DEFAULT_PARAMS` would silently
hand it ARC's solve-rate gates — a leak no presence check can see, because the default
puts the key there); (c) mode-gated clauses appended to the specialist prompts; (d) two
canonical artifacts the deploy mirrors and the role can read but not write:
`code/vision/` (the trainer) and `code/eval/vision_map_scorer.py` (the gate's
instrument, sha256-recorded per run like the judge prompt).

Two deliberate refusals shape the line. Ultralytics YOLO is unsupported **by licensing
design**: the vendor asserts AGPL-3.0 over fine-tuned weights and the embedding
application, so the catalog says so out loud, the orchestrator prompt makes it a red
line, and a repo test greps the vision code for the import. And the LLM judge is
unreachable **by measurement design**: COCO-style mAP is a fixed algorithm over fixed
annotations — the same class of instrument as the exact-grid gate, with zero judge
variance to argue about.

## 14. The admin console — one Lambda, three planes with different rules

![Console architecture](architecture-console.svg)

The dashboard is a single
Lambda serving the HTML and all **30 route handlers** across **9 tabs**, with **no build step
and no CDN**:
`frontend.html` is embedded at cold start and the page ships with `CSP connect-src 'self'`
plus the S3 origin the upload path needs. One artifact means the UI can never be a version
out of step with the API it calls — the failure mode a separately-deployed SPA invites.

Its design is four planes that deliberately do **not** share rules:

| plane | handlers | rule |
|---|---|---|
| **read** | 11 GETs (`/api/overview`, `/api/pipeline`, `/api/run`, `/api/observability`, `/api/cost-overview`, …) | public, aggregated server-side |
| **session** | 3 POSTs: `/api/login`, `/api/refresh`, `/api/refresh/revoke` | unauthenticated **by necessity** — these mint or revoke the credential |
| **write** | 14 POSTs: `/api/start-run`, `/api/cost-approval*`, `/api/finops-run`, `/api/optimize*`, `/api/native-rec*`, `/api/batch-eval`, … | Cognito at one chokepoint |
| **consult** | everything under `/api/tasks`, **both methods** — 2 GET handlers (`/api/tasks`, `/api/tasks/{id}[/approval|/readiness]`) and the POSTs `/api/tasks/{id}/{message,accept,close}` plus `/api/data-upload-url` | authed **and** group-checked; the only plane that invokes an agent |

An earlier version of this table listed `/api/tasks` in the read plane *and* in the consult
plane, and the code agreed with the first one: four consult **reads** — the thread list, the
thread itself, and its approval and readiness panels — were served anonymously on a public
API Gateway URL for the platform's whole life. `GET /api/tasks/{id}` returns the whole
DynamoDB item, which is the customer's transcript; `/approval` returns `approved_by`,
`cognito_sub` and `source_ip`, the identity fields the KMS signature exists to bind. The
cause was that the chokepoint was keyed on the **HTTP method** (`if method == "POST"`), so
the property this section boasted — adding a route cannot accidentally add an
unauthenticated *write* — was exactly true and exactly insufficient. The gate is now keyed
on the **plane**, as a path prefix (`_is_consult_path`): enumerating the four leaking routes
would have closed the hole and left the mechanism that produced it intact, so the fifth
panel added to a thread would arrive anonymous the same way.

**Every POST that acts on the platform is authenticated in exactly one place.** The router
resolves `_authed_user(headers)` once, before dispatching any POST, and returns 401 on
failure — so adding a route cannot accidentally add an unauthenticated write. It resolves to
a *user* rather than a boolean because two downstream checks need identity, not just
authentication: the approver-group test, and the never-self-approve test that compares the
approver's username to the requester's. Verified live, unauthenticated: `/api/tasks`,
`/api/start-run`, `/api/cost-approval`, `/api/data-upload-url`, `/api/finops-run` and
`/api/tasks/{id}/message` all return **401**, while `/api/overview` and `/api/cost-overview`
GET **200**. `GET /api/tasks` was in the 200 list when this was measured; it is a 401 now, and
that line is the artifact of the leak — the measurement was taken and written down as
confirmation of the design, because the design being checked was the one about POSTs.

**Three POSTs sit above that chokepoint, and naming them is part of the design.** `/api/login`
mints a session; `/api/refresh` restores one from the httpOnly cookie after a page reload (the
cookie *is* the credential, so demanding a Bearer token here would require a live session to
recover from having lost one); `/api/refresh/revoke` signs out, and refusing to revoke a
session because its access token already expired is backwards. An earlier version of this
section claimed Cognito ran on **every** POST — false in the flattering direction, the same
mistake as the §9 model split and the §11 VPC claim. `tests/test_console_routes.py` now derives
all four numbers from the router and compares the pre-chokepoint set against an explicit
allowlist, so a *fourth* unauthenticated POST fails the suite by name. A count alone would not
have caught it: a new unauthenticated write that replaced a session route keeps the count.

**Operational reads are public on purpose; customer reads never are.** Everything on the read
plane is already-reconciled operational fact — what ran, what it scored, what it cost. Gating
it would add friction to the thing an operator does fifty times a day while protecting nothing
that isn't in the diagrams. Authority is a different question from visibility, so on that
plane it attaches only to the writes.

The consult plane is where that reasoning stops, and reading it as a rule about *reads* rather
than about *operational fact* is what produced the leak above: a customer's conversation is
neither reconciled nor operational, and it is not in any diagram. So the consult gate checks
group membership too, not merely a valid token — otherwise any account provisioned to watch
the Pipeline tab could read every engagement in the account. 401 and 403 stay distinct there
for the same reason as on the write plane: telling an operator who is simply in the wrong
group that their session expired sends them round a re-login that cannot help.

**The cost gate is server-side, and advisory by configuration rather than by accident.**
`APPROVAL_LIMIT_USD` (default 2000) and `BUDGET_MODE` (`advisory`, or `blocking`) live in the
Lambda, not the UI: a gate a client enforces is a gate a client can skip. `advisory` names an
over-budget dispatch and lets it through with the estimate recorded; `blocking` refuses it.
Approvals are **KMS-signed and hash-chained** (`conductor_tools.sign_record`), carrying the
approver's identity and source IP, so an approval is evidence rather than a UI state.

**The signed plan is the authority on which models a run may use**, because model consent is
model-specific: approving a Fable-5 teacher at $0.05/1k output is not approving a DeepSeek-R1
one. So `seed_manifest` resolves `models` as `DEFAULT_MODELS` overridden by the plan, with
`params.models` allowed to fill roles the plan is silent about and **refused** where it
contradicts one the plan named. Before this the defaults simply won: run 68cfa9c8's manifest
carried `models.teacher = us.deepseek.r1-v1:0` while its signed plan said
`global.anthropic.claude-fable-5`, and the data-prep agent had to notice the contradiction and
pick the signed one *by judgment* — writing "top-level manifest 'models' field is stale
boilerplate" into the driver it generated. It chose correctly; that it had to choose at all is
the defect, because the decision a signature exists to settle was handed back to the model.
A disagreement is refused rather than silently resolved: it means the dispatch path and the
approval path disagree about what was bought, and guessing buys an unapproved spend that looks
authorized in every artifact afterward.

**That was only half the fix, and the other half is why one model may be named several ways.**
The precedence rule was right while the *field name* was wrong: the console form — the only
path a customer has to sign a plan — posts `plan.teacher_model` and `cost_model.py` prices the
run from it, while the resolver matched only `plan.models.teacher`. So a console-signed plan
arrived with `models` absent, which reads as *"the plan is silent about the teacher"*, and fell
through to the defaults: **priced as Fable 5, executed on DeepSeek-R1, with every artifact
agreeing.** A consent check that reads a different field name than the consent is written under
is not a check. `ROLE_ALIASES` therefore accepts every spelling of a role on **read** and
normalises to one role name — accepted rather than declared illegal, because three of the four
names sit in signed artifacts on S3 that cannot be rewritten, and a plan signed last week must
still dispatch as the model it approved. A role named twice with two different ids is refused;
so is a `models` key that is neither a role nor supply-chain provenance, because `teachr` used
to mean silence and silence spends. And a mirrored open-weight repo — the block where the
licence was read and the revision pinned — that is assigned to **no role** is refused by name:
a plan mirroring `meta-llama/Llama-3.2-1B` produced `student = Qwen/Qwen3-1.7B`, training on a
model nobody cleared while the cleared one sat unused in the mirror.

**The resolved consent reaches the agent turn as stage params.** `_run_stage` reads
`manifest.models` and injects the approved ids under the names the prompts already read
(`params.teacher_model_id`, `params.student_model_id`, `params.judge_model_id`) — one extra S3
GET per stage rather than trusting the dispatch event, because the manifest is where the
consent was recorded. Nothing used to write those params, so agents read an absent value and
fell back to the only model id in front of them: the one hardcoded in their own persona line.
Boilerplate standing in for consent, which is why no prompt names a model any more. A
caller-supplied value still wins — a remediation iteration may legitimately override — but it
is no longer the default, and roles the manifest is silent about are **omitted** rather than
defaulted, so a stage that needs a teacher and has none fails visibly instead of choosing one.

**The same authority rule covers every OTHER field of the plan, which took two more fixes to
learn.** The two above cured model consent and then the name it is written under, and both left
the rest of the plan behind: `seed_manifest` consulted `plan` for models and merged everything
else as `DEFAULT_PARAMS` overridden by `params` alone. So a signed industrial-defect plan priced
on `ml.p4d.24xlarge` with 40 000 samples and a `{"map50": 0.75}` gate **executed** on
`ml.g5.2xlarge` with 2 000 samples and ARC's `relative_solve_rate` gate; `pipeline_mode:
data_audit` was dropped, so a customer who bought a cheap audit had GPUs provisioned by the
`StartAt` Choice's `Default`; and the console's approve→launch forwarded no plan at all, having
scraped it for two integers. None of it is visible afterwards — the variance report joins the
estimate to the actuals and reads the gap as an **underspend** rather than as two different runs.

`PLAN_META_KEYS` now names the keys that are *about* a plan (prose, price, authorship) and
**every other field reaches `params`**. That is a denylist on purpose: an allowlist omits the
field nobody thought of, and the omission is invisible because a default takes its place, which
is precisely how `pipeline_mode` and `gates` went missing. A field a future orchestrator writes
arrives by default and must be *named* to be excluded, so the failure mode is a stage ignoring a
field rather than a run executing settings no human chose. The nested `data` block is flattened
one level out — data-prep's audit task reads `params.source_uri` flat, and an audit dispatched
from a signed plan used to arrive with no data URI at all — with an explicit top-level key still
winning, since a silent overwrite there is the same defect one layer in. Precedence and refusal
are the model rule verbatim: **`DEFAULT_PARAMS` < `params` < signed plan**, disagreements refused
by name against the *flattened* plan. This is also what makes the platform generic: `dataset:
arc-agi-2` and the `relative_solve_rate` / `format_validity` gates are fine as the fallback for
a run nobody planned, and they now lose to a plan naming a COCO dataset and a `map50` gate.

**Consumer half, again: a gate may not name its own bar.** The eval agent's gate task read
*"student judge-score >= 0.80 x teacher score"* out of its own prompt, so a detector run would
have been judged on ARC's metric. It reads `params.gates` now; a metric named there but missing
from the report is a **failed** gate, and `params.gates` absent entirely escalates to a human,
because an unnamed bar is a missing approval. Finetune and deploy likewise read
`params.training_instance` / `params.inference_instance` — the instance the run was *priced* on —
and must state which they chose and why when the param is absent.

**A stage's results go back to S3, so the next stage can read them.** Every specialist prompt
calls the manifest at `manifest_uri` *"the single source of truth"* and is told to read it first
and append its own results — and for a long time there was nothing there to read. The driver
assembled each finished stage's `{status, outputs, metrics, evidence}` into a local dict, handed
it to `write_run_report`, and dropped it; it had no `put_object` for the manifest at all. So the
run *report* carried every metric and the *manifest* carried none: `stages` was still `{}` after
a deploy stage reported an `endpoint_name`. The write is a **read-modify-write narrowed to
`stages`**, for two independent reasons. The driver is the *second* writer — `S3PipelineObjects`
grants the harness role `PutObject` on `runs/*` and 5 of the 7 prompts tell the agent to append
here — so a blind put would erase what the agent wrote during its own turn. And a driver that
could rewrite `models` / `plan` / `approval` / `params` is exactly the defect bugs #9, #20 and
#21 each were, so `IMMUTABLE_MANIFEST_KEYS` are taken from the copy on S3 and never from the
driver's. An absent manifest is **refused and reported**, never manufactured: a stages-only
document has no plan, no approval and no models, which reads downstream as a run nobody planned.
The refusal degrades to a reported warning like the report write above it — the task token is the
pipeline's only way to learn a paid-for stage succeeded, and nothing may withhold it.

**One entry per stage name is one entry too few.** `stages` is keyed by stage *name*, so the
entry the driver writes on `stage_complete` is the only record of that stage — and the next
task of the same stage, or the same stage in the next iteration, replaces it. Measured on
r5 (`run-20260811T101948Z-f9d34d27`, remediation loop, 2 iterations): the surviving manifest
holds `stages.finetune.metrics.iteration == 1` and an `eval` entry carrying
`delta_judge_win_rate_vs_i0` with no iteration-0 row left to subtract from. The
driver-verified i0 training losses and judge counts were gone; they survived on S3 only
because the eval agent happened to archive `report-i0.json` by hand. A loop whose entire
purpose is *"did the one targeted change help?"* could not answer that from its own manifest.
The fix is an **append-only `stage_history`**, not a re-keying: `stage_fact_params` and every
specialist prompt read `stages[<stage>]` by bare stage name, and the agents already write
their own thin `"<stage>.<task>.i<n>"` entries into the same map, so a driver writing that key
would race an agent for it and win or lose per run. The append lands on the list
`_save_manifest` just *re-read* from S3 rather than on the caller's snapshot — `stages` is
replaced wholesale and can lose a write that arrives during the turn, and `stage_history` must
not inherit that gap, because losing a record there loses it permanently while losing a
`stages` entry loses a duplicate of the latest one.

**An escalation that only the alert channel knows about is invisible to the report.**
`build_run_report` raises a critical finding for a stage whose status is `escalated`, and
nothing ever wrote that status into `stages`: `handle_escalate`'s durable records are
`runs.status` and (since #52) a DDB stage event, neither of which the report reads. So r5
escalated to a human at its iteration-1 gate and published `"findings": []` — the one run
that needed a person to look at it produced the report of a run with nothing to say. The
driver now appends to a second append-only list, `escalations`, and the report derives its
critical findings from it. Appended rather than written onto `stages[<stage>]["status"]`
because the escalating stage usually *has* a completed entry — r5's `eval` holds the scoring
task's judge counts — and overwriting it would trade a missing finding for a destroyed
measurement. The escalation caller passes `manifest=None`, which makes `_save_manifest`
append-only: a path with no stage results of its own must not be *able* to restate any. It is
also guarded on the URI naming the same run as `run_id`, because a triage's `manifest_uri`
names the **subject** run while its `run_id` is the conductor's own.

**A report that cannot read a status must say so, not count it as a failure.** `stages` has
two writers with two vocabularies — the driver writes `completed`, the agents write
`complete` / `launched` — and only the driver's was counted. r5 published
`{"total": 14, "passed": 3, "failed": 0}` for a run in which every stage succeeded: 11 of 14
counted as nothing at all, which the console renders as a run that mostly did not pass. Both
vocabularies are now read, a `launched` job counts as `in_flight` (not a pass and not a
failure — that outcome does not exist yet), and `unrecognized` is published so the four
sub-counts reconcile to `total`. The gap between `total` and `passed + failed` is exactly
what hid this for 14 runs.

**A digest the claimant chose is not a check.** `deploy/03_storage.py` mirrors the canonical
pairwise judge prompt to `code/eval/judge_prompt_pairwise.md` so that "comparing two runs is a
digest comparison rather than an argument" — and nothing compared the digest to anything. The
scoring agent wrote `judge_prompt_sha256` beside its own `judge_score`, which gives the
attestation exactly the standing of the number it was supposed to corroborate. r5 is the
failure it exists to catch: the eval prompt said "fixed judge prompts" and fixed none, that run
authored its own A-or-B-only instrument, and its `judge_ties: 0` was read as a property of the
student when it was a property of a prompt that offered no tie. The driver now fetches the
canonical object itself, hashes its bytes, and files the comparison in `manifest.attestations`
— **beside** the stage entry rather than inside it, because that entry is the agent's claim
space and a measurement stored inside a claim stops being independent of it. Three outcomes,
not one: a **mismatch** (the score is not comparable to any other run's, `high`), an **absent**
digest (the score names no instrument, `medium`), and an **unreadable** canonical object (the
check did not run, `medium`) — reporting the third as the first would blame a run for a gap in
the deploy. Whoever owes the attestation is derived from the judge numbers in the metrics, not
from a `(stage, task)` name list, because the eval prompt lets a small enough prompt set be
scored inside `evaluate` instead of `score` and a name list would quietly stop checking that
run. The driver never substitutes the canonical digest for a missing claim: that converts an
absent attestation into a false one.

**A second derivation of one verdict is a second verdict.** The console's gate table recomputed
each row as `actual >= threshold` while `agents/eval`'s gate bullet decides an interval-bearing
metric on its Wilson bounds — pass only if the lower bound clears the bar, fail only if the upper
bound is below it, escalate otherwise. The repo's own console fixture is the demonstration:
`judge_score` 0.48 against a 0.45 bar with CI [0.40, 0.56] rendered **PASS**, on a run whose
`gate_passed` was `false` because the agent escalated it as borderline. `gate_row` now derives
the verdict the way the pipeline does, keyed off `<name>_ci_low` / `<name>_ci_high` rather than
off the string `judge_score` — whether a metric is decided by an interval is a property of what
the report carries about it, and a name list would drop the next interval-bearing metric back to
its point estimate. It also publishes a `status` instead of one tri-state boolean, because three
different situations were sharing a blank cell: a **borderline** the pipeline refused to decide
(`passed: null`), a gated metric the eval report never carried — which the eval prompt defines as
a **failed** gate, not an undecided one (`not_measured`, `passed: false`) — and a run that died
before eval ran at all (`not_evaluated`, `passed: null`). A value no threshold can be compared
against is `unreadable` and fails closed. On `run-phase2-main-0001` the old table said "gate
failed" above two blank rows and so could not say *which* gate failed, the one question the table
exists to answer. Whether eval reported is keyed on the presence of the stage entry's `metrics`
key, not on the entry and not on the dict being non-empty: the driver always writes that key on
completion and the agents write mid-stage entries without it, so on
`run-20260811T165529Z-ce628817` (`status: inference_in_progress`) an entry check would fail both
gates while the inference job was still launching, and an emptiness check would read a report
that arrived carrying nothing as never having been measured.

**An authorization boundary that can be skipped is not a boundary.** `service_launch_run` verifies
the KMS signature on the approval record before a dispatch, and it read
`if kms is not None and not verify_record(kms, approval)` — so passing no client skipped the check
rather than refusing the call. The bypass was total, not partial: executed directly, an approval
record carrying **no signature at all** and a made-up `approved_by` returned `{"ok": true,
"run_id": ...}` and invoked start-pipeline. Nothing depended on it — the driver passes `_kms(c)`
and the console passes its module-level client, so its only effect was that the one check standing
between a forged "a human said yes" and a real run disappeared exactly when the verifier did. It
now returns a refusal naming the missing verifier, which is the same rule the deploy read-back and
the judge-instrument attestation already follow: **a check that could not run is not a check that
passed**, and on this boundary the difference *is* the boundary. The file also had none of the 207
negative controls, which is why the bug survived a suite that tests the signature thoroughly: the
controls now break each guard in turn — the fail-open restored, an unsigned record read as
verified, the stored `record_sha256` trusted instead of re-derived, a raising verifier read as
valid, a key dropped from `SIGNED_KEYS`, canonicalization following the record's own key order,
the digest swallowing the signature it signs, the signed plan hash no longer compared to S3, and
every audit record linking to genesis. The tests those controls verify include one written only
because the double was too kind: `FakeKms.verify` returns `{"SignatureValid": false}` where real
KMS *raises* `KMSInvalidSignatureException`, so `verify_record`'s `except` branch — the branch
production takes on every tampered record — had never been executed by any test.

**A count that does not reconcile is the cheapest audit an instrument has.** The gate reform gave
eval two acceptance layers — an in-distribution set that gates and an OOD set that is measured and
never gates — writing into one `evaluation/judge_details.jsonl`. Both files number their rows from
zero, and that is enough to break the arithmetic without breaking a single judgment. Measured on
this exact analysis, run offline: 40 of 137 items shared an index across the layers, so keying on
the index paired an ID item's A-position verdict with an OOD item's B-position verdict and dropped
40 ID items out of their own layer. The aggregate reported **57 items for a 97-item layer** and
every judge call behind it had been made against the correct content. Bookkeeping, not judging,
which is exactly why nothing looked wrong. The score bullet now requires `layer` and `item_id` on
every row and forbids keying a join on a row number or a per-file index — the fields alone are
decoration, since the offline run recorded the layer too and still cross-paired. The load-bearing
half is the reconciliation: `judge_n` counts *scored* items, so a lost item shrinks the denominator
silently, and a Wilson interval on a shrunken denominator is **narrower** around the wrong sample —
the one direction in which a bookkeeping slip becomes a *decisive* gate verdict on a set nobody
chose. Each layer therefore reports `items_in_layer`, read from its own acceptance file, and asserts
`judge_n + judge_unscorable == items_in_layer`; a mismatch is `escalate_human` with both numbers,
not a footnote. The sibling defect in the same first pass is already pinned: at `maxTokens: 400` a
judge that reasons by default spent its whole budget inside `reasoningContent` and returned an empty
text block on 30 of 274 calls, so a **truncated** judge would have been scored as an undecided one.

**A file nothing deploys is not a component, and a guard that names its path cannot notice.**
`pipeline/training/train_qlora.py` carried three deliverability rules, a liger-kernel preflight
and a model-mirror integrity check; 15 unit tests asserted them; a verification doc recorded five
real `ml.g5.2xlarge` jobs behind them, including a checkpoint reaching S3 mid-run and a wall-clock
budget trip that ended `Completed`. It was mirrored **nowhere**. `deploy/03_storage.py
ensure_code()` uploads `pipeline/training/distill/`, and the copy that lived there — authored by
the FINETUNE agent on one run and promoted to "canonical" because it trained an 8B student in
929s — carried `save_strategy="no"`, the exact line the other file's docstring names as the cause
of run `e1g6`'s 43 GPU-minutes for zero artifacts. Every run downloaded the copy with none of the
rules, and every guard read the copy no run could reach, so the suite was green about a file that
was not a component. Measured on the four real training jobs the system has launched: `save_steps`,
`max_train_seconds` and `CheckpointConfig.S3Uri` were **unset on 4 of 4** — both hard FAILs of
`validate_job_config.py`, which sat in the un-mirrored directory with **zero callers** and was
named by no prompt. There is one trainer now, at the path `ensure_code()` mirrors, and the preflight
ships beside it so the deploy carries it; the launch prompt names its S3 key, tells the agent to run
it, and forbids launching a FAILing payload. The guards no longer write the path down — the
deliverability tests, the architecture diagram's trainer link, and the prompt↔`argparse` contract
check all derive it from `ensure_code()`'s own literal path parts, so pointing the mirror at an
abandoned copy reds the suite instead of quietly retargeting it. The remaining exposure was never
theoretical: `run-20260812T035446Z-dedaa965-i0` died 386 seconds in on the *same*
`UnboundLocalError: trainer` that killed r6a — the second time an improvised trainer has cost a run
— and every job so far survived only because it finished inside a 7200s `MaxRuntime` it had no
save point behind.

**A layer nothing gates needs its floor written down twice, because nothing else will notice.**
The dual-layer gate blocks a deploy on the in-distribution layer *alone*; the OOD layer is measured
and reported and never gates. That trade is honest only while the layer is really being measured,
and neither end of it was enforced. On the way in, data-prep's `curate` decontaminated the training
corpus against `params.customer_eval_uri` and named `params.ood_eval_uri` nowhere — while
`_plan_params()` flattens all of `plan.data`, so the param arrived at data-prep on every run and no
task read it. The gated layer is the one that needs the protection least: overlap there inflates a
number something checks, while overlap in the report-only layer fails nothing at all and simply
reads **higher**, which is the evidence anyone would cite to say the student generalises — and
"bigger student" and "synthesis closes the OOD gap" were both refuted 0-3 in this system's own
research pass, so the OOD number is the object of the experiment, not a decoration. Measured with
`curate`'s own rule (prompt trigram-Jaccard ≥ 0.6) against the two live files: **0 of 40** OOD rows
overlap the 300-row source, max 0.1882, 23 OOD categories against 12 with an empty intersection.
Clean — by hand, and recorded nowhere, which is the other half of the gap: a 0 that was *written*
is a different fact from a 0 nobody computed. On the way out, `params.ood_eval_uri` set with no
`report.json.ood` was byte-identical to a run that never asked for the layer — the driver reads only
`gate_passed`, and the console drew the block on presence alone, six lines under a comment that
draws exactly that distinction for the gate rows. So `curate` now decontaminates every acceptance
layer `CUSTOMER_DATA_PARAMS` names and records one drop count per URI; the `score` bullet makes an
absent `ood` object illegal and gives the failure path something to write (`items_in_layer`,
`judge_n`, `ood_error`) instead of silence; and the run view says **NOT REPORTED** when a signed
plan named an OOD set and a completed eval carried none — conditioned on the eval having reported at
all, because an alarm on every run that is merely mid-inference is how an operator learns to ignore
the one that is real.

**An `n` is a claim with exceptions, and a page that prints the claim without the exceptions is
printing the flattering half.** `judge_n` counts *scored* items only. A judge call can fail to return
a verdict for reasons that have nothing to do with the answers, and that missingness is not random:
on the 8B run, 9 of 274 (item, position) slots came back `content_filtered`, retrying recovered 5,
and the 4 that stayed unjudgeable clustered on the credential / MFA / access categories where the
student scored 0.000. So the interval over the survivors is *narrower and higher* than the interval
over the layer, and the eval gate bullet answers that by recomputing its verdict with every
unscorable item imputed as a win, then a loss, then a tie — escalating only if those three disagree,
which on the 8B run they did not. The console ran two of that rule's three clauses: it decided PASS
off the survivors-only bound, and it displayed `n=94` for a **97-row** layer with the 3 excluded items
nowhere on the page. Near the bar those are the same defect twice — survivors at 0.5532 with a lower
bound of 0.4520 clears a 0.45 gate, and the same rule with the 3 items imputed as losses gives 0.5361
with a lower bound of 0.4374, which is borderline — so the page painted **PASS** on the one verdict
the pipeline had escalated rather than reach. The row now runs the third clause, carries
`judge_unscorable` and `items_in_layer` beside the interval, and re-checks D8's reconciliation
(`judge_n + judge_unscorable == items_in_layer`) rather than trusting it: the agent is required to
refuse a score that fails it, and this page is the only reader that can catch a report which asserted
it and got it wrong. A denominator that does not reconcile gets its own verdict, because a
bookkeeping failure and a student that missed a bar call for entirely different work.

**A borderline band measured in absolute distance swallows the pass region when the bar sits within
the band's width of the metric's ceiling.** The gate reform gave interval-bearing metrics a Wilson
rule, but it did not delete the prose band — it narrowed its scope, so every gate that is *not*
`judge_score` was still decided by "borderline means within 0.05 of the threshold, escalate with the
numbers." All three r6 plan drafts gate on `{"judge_score": 0.45, "format_validity": 0.95}`, and
`format_validity` is a proportion: its ceiling is 1.0, so `bar + band` = 1.00 and the **entire
passing region [0.95, 1.00] lies inside the borderline band.** One malformed answer out of 97 —
0.9897, a gate comfortably met — is an `escalate_human`, and no run carrying that gate can reach a
*decisive* gate pass at all, so none can reach `Complete` without a human verdict. It is masked
only because `judge_score` fails decisively first; it stops being masked exactly when the student
starts passing. A perfect rate is worse than ambiguous: `1.0 - 0.95` is `0.050000000000000044`, so
whether it is "within 0.05" is decided by binary floating point on the one value an operator is
most likely to see. The fix moves no bar. `format_validity` is a count over a countable
denominator, so the score bullet now requires it to report `format_validity_ci_low` / `_ci_high`
and `format_n`, which routes it through the *existing* bounds branch — that branch is keyed off
`<name>_ci_low` presence and derives the family as `name.rsplit("_", 1)[0]`, precisely so the next
interval-bearing metric is not dropped to a point estimate — and 97/97 becomes a decisive pass at a
lower bound of 0.9619 while 96/97 becomes an honest [0.9439, 0.9982] borderline. The band survives
as the fallback for a metric with no denominator, with the check it was missing: if `bar + band`
reaches the metric's ceiling, the agent says so with the arithmetic and escalates **once**, naming
the bar as the defect, rather than rediscovering it as a surprise escalation every run. The console
had the mirror-image defect in the branch the second-derivation fix did not touch — no band at all,
so it painted PASS across the whole band *and at the bar itself*, where the distance is 0 and the
rule is maximally borderline — and two existing assertions pinned that behaviour as correct. Both
sides now read the number out of one sentence:
`test_the_scalar_band_is_the_one_the_eval_prompt_states` regexes the band out of the eval prompt, so
a console band and a prompt band cannot drift apart on the runs nobody re-reads. A guard whose
assertion encodes the shipped behaviour is not evidence that the behaviour is right.

**The facts a run discovers travel like the consent it was signed with.** `MODEL_PARAM_FOR_ROLE`
carries what a human *signed* into the stages that must obey it; `STAGE_FACT_PARAMS` carries what
the run itself *produced* into the stages that must measure it. `params.student_endpoint` — read
by eval and by monitor — is the case that named the pattern: an endpoint name does not exist
until the deploy stage creates one, so no plan can be signed with it and no default can stand in
for it, and it was written by nothing at all. It is read from the producing stage's own report in
`stages`, never from `models`, because `models` is the record of model *consent* and the driver
must not write it. Absent facts are **omitted, not defaulted** — a stage that needs the endpoint
and finds no param must fail visibly, because a CloudWatch metric attributed to the wrong
endpoint is worse than a missing one: it reads as evidence. This is also what a self-iterating
pipeline needs to exist at all. An agent asked to diagnose a run used to have only its own turn
to look at, and a pipeline whose stages cannot read each other's results cannot iterate on a run
— it can only redo it.

**Every param a prompt reads is pinned to the mechanism that writes it.** Bugs #20, #21 and #22
were one shape found three times by hand, so the fourth is derived instead:
`test_every_param_a_prompt_reads_has_something_that_writes_it` enumerates all 25 `params.X` the 7
prompts read and classifies each as supplied by `DEFAULT_PARAMS`, a signed plan, the driver
(`MODEL_PARAM_FOR_ROLE` or `STAGE_FACT_PARAMS`), or the dispatch event. A param appearing in a
prompt with no entry fails, and an entry no prompt reads fails too — the second direction catches
wiring that has gone dead. The dispatch category is the escape hatch, so it is short and every
member must be a value that *cannot exist* before the invocation carries it, not merely one
nobody has wired yet.

**The Tasks tab is the only plane that talks to an agent**, and it is the customer-facing
half of the product: one thread per engagement, where `llmops_orchestrator` runs its consult
protocol, the customer hands over data via a **presigned** `customer-data/` upload (the key is
always server-chosen — a client-supplied key is a path-traversal write), and the output is a
priced plan whose acceptance is signed. The console signs those writes; the pipeline's own
role gets `customer-data` **read-only**, because a pipeline that can rewrite customer data can
destroy the held-out set its own gates are judged against.

**The run view reads the timeline as two bounded queries, not one filtered list** — stage
events under `sk < "A"`, parked verdicts under `sk begins_with "directive#"` — and renders
verdicts in their own panel labelled *delivered* / *parked* / *never delivered*. §3 has the
reasoning: a prefix is not a filter, and a verdict that could never be read must not look like
one an agent acted on.

**A window taken before the ordering is a window on hash order.** Every list on the read plane
used to `scan(Limit=N)` and *then* sort by time, which reads as "the newest N" and is not: a
Scan's item order is documented as unspecified, so `Limit` trimmed whatever fell past the page
boundary and the sort merely arranged the survivors. Measured on the live `llmops-tasks` table
(35 rows, `Limit=25`): **6 of the 25 newest consultations were missing**, one of them in status
`error` and one a `drafting` plan waiting on a human signature, while 6 older threads occupied
their places. The reader now pages the whole table, orders in Python, and windows **last** —
correct because these tables are small (tens of rows, ~240 KB) and because neither runs nor
tasks has a time-sorted GSI to query instead; a full Scan is the cheapest honest read available
and the alternative would be a schema change to serve a page that already fits in one.

The bound that replaces `Limit` is `SCAN_PAGE_CAP = 8` pages, and the part that makes it safe is
that **the cap is rendered**. A list silently capped at N looks exactly like a list that
contains N, so `truncated` and the rows-read count travel to the frontend and the page says
*"newest 25 of 35 read"* — the same defect one level up, and the one this repo keeps paying for.
`_timeline` reads `limit + 1` rows in reverse so *"older history exists"* is a fact rather than
an inference from `len == limit`, which cannot tell a table holding exactly `limit` rows from
one holding more; it then re-sorts ascending, because the frontend paints `evs.slice(-25)` and a
reverse window handed over unsorted would paint the **oldest** 25 of the newest 100 — a window
right at neither end. Its two halves also disagreed about direction: the events half read
forward while the directives half already read backward, in one function. That was harmless
while a run held ~16 events and stopped being harmless when §7's `WAIT_ROW_CAP = 12` rows per
stage invocation raised the ceiling to ~150 across a harness — **the previous fix pushed the
real count past this one's window**, which is the kind of interaction no single-change review
sees.

The layout of all three diagrams in this document is enforced, not eyeballed:
`tests/test_svg_geometry.py` fails the build if any two wires cross, if two wires share a
corridor (which draws as one wire and silently loses a connection), if a wire passes through a
card, if two cards overlap, or if a committed SVG no longer matches
`docs/gen_architecture_svg.py` — the diagrams are generated and must never be hand-edited.
