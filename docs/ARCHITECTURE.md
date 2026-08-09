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
— plus the loop-only `RemediateFinetune` and the audit-only `DataAudit`:

```
DataPrepGenerate → DataPrepCurate → FinetuneLaunch → FinetuneAnalyze → EvalGenerate → EvalScore → EvalGate
                                                        │ (gate fail)
                              RemediateFinetune ←───────┘   … then, on gate pass:
             Deploy → SmokeTest → MonitorHealth → Teardown → MonitorReport
```

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
| `checkpoint` | turn budget nearing, progress persisted | re-invoke the same session to continue (self-reinvoke if the Lambda itself is near its limit) |
| `escalate_human` | out of budget or out of authority | SNS notification, run marked `escalated`, `EscalatedToHuman` event, task token failed |

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

- `_maybe_failover_model` hot-swaps a model after a vendor 5xx burst and the retry
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
skip the structured call), the driver re-asks up to 2 **consecutive** times in the same
session, then fails the stage as `MissingStageComplete` — narration is never promoted to
success. Any serviced tool call re-arms the budget: what it counts is an agent that has
stopped speaking protocol, not one that slipped twice an hour apart and recovered. Every
fleet prompt also states this contract explicitly as a TURN-END INVARIANT naming that
harness's own terminal tools, with a write-first rule (artifacts land in S3 before the
call that claims them) — a guard test derives both directions from each harness's
declared tools, so the sentence cannot drift from the tool list.

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
`IncrementIteration` → `RemediateFinetune` → back to `FinetuneAnalyze` →
`EvalGenerate` → `EvalGate`. The loop rejoins **above** the generator, not between it
and the gate: rejoining below would have iteration 2 re-gate iteration 1's report, and a
remediation that changed nothing could "pass".
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
token-flood generator (6 harnesses then, 7 since `llmops_finops`, × agent loops × long
streams). ~12 Fable 5 5xx bursts
recurred across a single day. Design rules (full text in [AGENTS.md](../AGENTS.md)):

1. Every harness has a fallback chain: `global.anthropic.claude-fable-5` →
   `global.anthropic.claude-opus-5` (same family, zero prompt changes). Hot-swap via
   `UpdateHarness`, ~15 s to READY; sessions survive the swap.
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

## 11. VPC posture for production

Harness configs (`agents/*/harness.json`) run PUBLIC-network for iteration speed. The
VPC itself is built by `deploy/02_network.py` and the Lambdas can run **VPC-isolated with
interface endpoints — no internet egress**.

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

## 13. The admin console — one Lambda, three planes with different rules

![Console architecture](architecture-console.svg)

The dashboard is a single
Lambda serving the HTML and all **30 route handlers** across **9 tabs**, with **no build step
and no CDN**:
`frontend.html` is embedded at cold start and the page ships with `CSP connect-src 'self'`
plus the S3 origin the upload path needs. One artifact means the UI can never be a version
out of step with the API it calls — the failure mode a separately-deployed SPA invites.

Its design is three planes that deliberately do **not** share rules:

| plane | handlers | rule |
|---|---|---|
| **read** | 13 GETs (`/api/overview`, `/api/pipeline`, `/api/run`, `/api/observability`, `/api/cost-overview`, `/api/tasks`, …) | public, aggregated server-side |
| **session** | 3 POSTs: `/api/login`, `/api/refresh`, `/api/refresh/revoke` | unauthenticated **by necessity** — these mint or revoke the credential |
| **write** | 14 POSTs: `/api/start-run`, `/api/cost-approval*`, `/api/finops-run`, `/api/optimize*`, `/api/native-rec*`, `/api/batch-eval`, … | Cognito at one chokepoint |
| **consult** | `/api/tasks`, `/api/tasks/{id}/{message,accept,close}`, `/api/data-upload-url` | authed **and** group-checked; the only plane that invokes an agent |

**Every POST that acts on the platform is authenticated in exactly one place.** The router
resolves `_authed_user(headers)` once, before dispatching any POST, and returns 401 on
failure — so adding a route cannot accidentally add an unauthenticated write. It resolves to
a *user* rather than a boolean because two downstream checks need identity, not just
authentication: the approver-group test, and the never-self-approve test that compares the
approver's username to the requester's. Verified live, unauthenticated: `/api/tasks`,
`/api/start-run`, `/api/cost-approval`, `/api/data-upload-url`, `/api/finops-run` and
`/api/tasks/{id}/message` all return **401**, while `/api/overview`, `/api/tasks` and
`/api/cost-overview` GET **200**.

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

**Reads are public on purpose, writes never are.** Everything on the read plane is
already-reconciled operational fact — what ran, what it scored, what it cost. Gating it would
add friction to the thing an operator does fifty times a day while protecting nothing that
isn't in the diagrams. Authority is a different question from visibility, so it attaches only
to the writes.

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

The layout of all three diagrams in this document is enforced, not eyeballed:
`tests/test_svg_geometry.py` fails the build if any two wires cross, if two wires share a
corridor (which draws as one wire and silently loses a connection), if a wire passes through a
card, if two cards overlap, or if a committed SVG no longer matches
`docs/gen_architecture_svg.py` — the diagrams are generated and must never be hand-edited.
