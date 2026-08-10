# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/); versioning: SemVer.

## [Unreleased]

### A page addressed to the conductor is an alert filed where nobody will look

`triage_event_from_bus` has always passed the stuck run down as
`params.escalation.run_id`, and the comment above `TRIAGE_STAGE` has always said so.
Nothing read it. Every consumer took the subject from the model's own tool arguments and
fell back to `event["run_id"]` — which on a triage is `triage-<subject>`, the one id that
must never be the subject. Measured over every `HumanPaged` row in `llmops-stage-events`
(12 rows, full scan, `ScannedCount == Count`): **3 are filed under a `triage-` id** —
`86ab8a14`, `c8b13faa`, `b56281da`, each an ARC-2 lineage run that died with its
scientific work complete. The alert fired; the audit trail points at the conductor. This
is the failure the #72 backstop was built to end and could not see, because the backstop
only asks *whether* a page happened.

- `orchestration/harness_driver/handler.py`: one `triage_subject(event)` reads the subject
  from the invocation. `handle_page_human`, the `resolve_escalation` branch and
  `_backstop_page` all call it. The same derivation was spelled three ways, and
  `_backstop_page`'s copy was the correct one — which is why the backstop's own pages are
  the ones filed properly, and why the symptom looked intermittent rather than total.
- **A `required` in a tool schema is a request, not an enforcement.** `run_id` is not in
  `page_human`'s `required` list at all, and *is* in `resolve_escalation`'s — a model
  omitted it anyway. Neither fact is the fix: the driver already knows the subject, so the
  agent's copy is redundant and is now consulted only when the event carries none (the
  console chat path, where `event["run_id"]` **is** the subject).
- **A resolve naming no run reported `resolved`.** `if subject:` skipped `put_directive`
  *and* the reachability check, then fell through to `{"status": "resolved"}` — a status
  inside `TRIAGE_ANSWERED`, so the backstop stayed quiet too. An unanswered escalation
  reported as answered, its only record filed under the conductor. It is now rejected back
  into the same turn naming `page_human`. The returned `run_id` is the subject, not the
  agent's copy, because the console renders that dict.
- A page whose subject cannot be derived is still recorded, under the triaging run and
  saying so (`run_id: ""` in the brief). The alternative is a `put_item` on an empty
  partition key — a `ValidationException` turning a successfully-published page into a
  crashed invocation, which is the swallow #72 exists to stop.
- Tests: 8, and the pre-existing addressing test is now parametrized over all four argument
  shapes a conductor can produce. It had used only the shape that works on main, which is
  precisely why it passed while the bug shipped. Negative controls `m141`/`m142` confirm
  both guards go red when the fix is reverted.
- **74 of the 164 negative controls had not run in some time, and the exit code said
  "one failure".** Found by running the full harness to check the two new cases. An anchor
  that drifts raises out of its `mutate`, and the raise was uncaught: it terminated the
  loop, so `m70` going stale (README corrected from 6 Lambdas to 7 two PRs earlier, the
  mutation left behind) meant **cases 70–143 never executed at all** while the process
  exited 1 for a single named reason that looked like the whole story. A broken anchor now
  fails that one case and every case still gets its turn, which immediately exposed 6 dead
  controls — `m70`/`m71` and `m15` (the 6→7 Lambda correction) and `m134`–`m137` (the four
  redaction coverage counts, stale since the repo grew from 161 tracked files to 163).
- **A control that hardcodes the number it tests for staleness has the defect it exists to
  catch.** All four redaction controls named a literal count, so the ordinary act of
  correcting that comment retired the control. They now read the number off the file and
  decrement it. `m137` decrements only the *current* half of "N files became M" and skips
  the value equal to the historical half: the live pair is 162 → 163, where a plain
  decrement yields 162 → 162, which the guard rejects as "not a change at all" — red for
  the wrong reason proves nothing about staleness.
- **A guard nobody runs per commit is a guard that expires quietly.** The only thing that
  checked a control's anchor was the 5-minute runner.
  `test_every_negative_control_still_matches_the_code_it_mutates` now calls every `mutate`
  against the real file text in memory and requires a change, in ~1.7 s with nothing
  written to disk. It caught its first
  regression within the same commit: guarding the runner's mutating loop behind
  `if __name__ == "__main__"` — required so the test can import the module without
  installing signal handlers over pytest's or deleting a killed run's recovery journal —
  moved `_restore_from_journal()` from column 0 to column 4 and killed `m85`'s anchor.
  Control `m143` covers the new guard.
- **A control that reads git history passes locally and fails only in CI.** The new guard
  above ran green on a full-depth worktree (991/991) and went red on the PR, naming `m127` —
  which read like a seventh drifted anchor and was not. `actions/checkout@v4` clones at
  **depth 1**: `m127` recovered this repo's account id with `git log --all -S 'arn:aws:iam::'`
  over historical blobs, and with one commit in the clone the walk found nothing, so the
  control raised its own "could not reconstruct" assert. Measured: `git clone --depth 1` of
  this branch shows **1** commit. Depth is only the shallow half — history is the wrong
  *subject* for a control, and that version was self-expiring, its own assert saying to delete
  the control if the id ever left history, i.e. the check evaporates exactly when the repo
  gets cleaner. `m127` now manufactures its precondition instead of excavating it: it adds a
  fabricated id's digest to `REAL_ACCOUNT_DIGESTS`, making that id watched by the only
  definition the scanner has, and plants the same id as two adjacent literals. The property
  under test is unchanged — "no split that reconstructs a *watched* id survives" — and the
  real id is now never spelled, never looked up, and never needed.
  `test_no_negative_control_depends_on_commit_history` parses the runner's AST and rejects
  any `git` argv naming a history subcommand (`log`, `rev-list`, `blame`, `merge-base`, …)
  while allowing the depth-insensitive index reads (`ls-files`, `show :path`,
  `diff --cached`) the other controls legitimately use. The fabricated id is itself split
  across two literals — spelled whole it would be a 12-digit run in a tracked file, moving
  the run counts `redaction_scan.py` derives and breaking its stated invariant that the
  *distinct* count never moves. A control may not change the measurement it sits beside.
- Corrected against live SNS, not restated: `llmops-escalations` no longer "has zero
  subscribers". One confirmed email recipient; 2026-07-29..08-08 the topic published 15 and
  delivered 11 with 0 failures, the 4 undelivered all predating the 2026-08-02
  confirmation. `handle_escalate`'s reason for not gating on SNS is unchanged and stronger:
  a channel that now works is still the one that fails on a throttle.

### A task token Step Functions has already discarded is an answer, not a crash

`resume_pipeline` has known this since 2026-07-29. The driver did not, and it cost four
invocations. `TaskTimedOut: 'Provided task does not exist anymore'` came out of the
re-asks-exhausted settle in `_run_stage`, the `handler()` wrapper re-raised it, Lambda
marked the asynchronous invocation failed and **retried it twice** — 2026-08-09 at
05:50:48Z, 05:52:03Z and 05:54:28Z are one incident, plus one earlier at
2026-08-05T15:39:51Z. Every retry was a fresh **billed** AgentCore turn re-running an
agent whose stage had already been decided, against a token none of them could settle. The
stage's verdict, its `PipelineFailed` event and its S3 artifacts were all already correct;
the only thing left to do was tell a state machine that had stopped listening.

- `pipeline/contracts/task_tokens.py` (new): `TASK_GONE_CODES` + `is_task_gone()`, imported
  by both Lambdas that settle tokens they did not park and defined by neither. The
  constant existed in `resume_pipeline` alone; copying it a second time is the defect this
  module exists to prevent — the driver's four settle sites and resume's one must agree
  about what "gone" means, and two constants in two files agree only until someone edits
  one of them. Matched by botocore error CODE, not exception class (the typed classes hang
  off a live client, unusable under an injected double) and not bare `Exception` (which
  would swallow the throttles and 5xx where the settle may yet succeed, stranding the token
  for its full `TimeoutSeconds` — 86400s, a day).
- `orchestration/harness_driver/handler.py`: one `settle_token()` funnel; all four settles
  (`stage_complete`, `escalate`, the crash-path report, re-asks-exhausted) go through it.
  Fixing only the one that crashed would have left three, and the next one hit would have
  read as a new bug — so a derived test asserts no `send_task_*` call in the file bypasses
  the funnel. `output=` picks success and its absence picks failure, so a caller cannot
  report success on a failure path by passing a wrong value.
- `orchestration/resume_pipeline/handler.py`: imports the shared definition, drops its own.
- `deploy/07_lambdas.py`: the bundle's module list is now **derived** from what the handlers
  import, read out of each `except ImportError:` branch with `ast` — that branch *is* the
  flat bundle layout. The list was hand-maintained, so adding `task_tokens.py` and importing
  it from two handlers would have deployed clean and killed the driver at cold start on
  `ModuleNotFoundError: task_tokens` — on the code path added to stop it dying. Same failure
  class as the hand-copied `env_keys` list, same cure. An unresolvable fallback import is
  now a refusal at build time, because `update_function_code` returns 200 for a zip that
  cannot import itself.
- Tests: 17, mutation-verified six ways (no fix; bare-`except` swallow; one settle left
  unguarded; bundle back to a hand-list; `flat_imports` back to the indentation regex —
  which matched 22 names across the seven handlers, since indentation says *nested*, not
  *nested in the fallback*; and each Lambda re-defining the constant locally).

### An unanswered triage now reaches the owner; the rejection stops naming a door that is painted on

Three layers of one defect. Together they made the escalation answer channel a total
loss that looked intermittent: **11 of the 11 directives ever parked in
`llmops-stage-events` are `deliverable: false, delivered: false`** — the channel has
never once delivered a verdict — and of the 9 runs whose escalation was triaged between
2026-08-05 and 08, **4 produced no `HumanPaged` event at all.**

- **The rejection named an exit that cannot open.** When a verdict is undeliverable the
  driver rejects it back into the same turn naming `launch_run` or `page_human`. But
  `service_launch_run` refuses without a KMS-verifiable approval record, and a triage
  built by `triage_event_from_bus` has none: nothing in the repo has ever written
  `params.approval_context` (a read with no writer), and `approval` is not a declared
  property of `launch_run` in `agents/orchestrator/harness.json`, so the agent cannot
  supply one either. New `dispatch_is_possible(event)` decides, and when dispatch is
  impossible the reason says so and names `page_human` as the only exit that changes
  anything. With a signed approval present the advice is unchanged — pinned by its own
  test, because a guard that amputated the working case would hand the owner decisions
  the conductor was authorized to make.
- **On the state-machine path the conductor is the ONLY line to a human, not the first.**
  `EscalateFail` is a bare `events:putEvents`; the `llmops-pipeline` bus has exactly one
  rule (`llmops-escalation-triage`) with exactly one target (this driver); there is no
  SNS anywhere after it. So a triage that ends without resolving, dispatching or paging
  tells nobody: the run row reads `failed`, the execution reads FAILED, the only trace is
  a log stream. New `_backstop_page` pages the owner when the outcome is not in
  `TRIAGE_ANSWERED`, stating plainly that the page is the driver's backstop and not the
  conductor's judgment. It wraps the `return` in `handler` rather than a branch inside the
  loop, so it covers every way a triage can end unanswered — prose after the re-asks, an
  unsupported tool, a rejected page, a `stage_complete` that decided nothing — plus the
  crash path, where a bus triage has no task token and `send_task_failure` therefore
  reaches no one. Best-effort by construction: a page that cannot be sent must not turn a
  merely-unanswered triage into a crashed invocation.
- Live casualties, each an ARC-2 lineage run that died with its scientific work complete
  and its owner never told: `run-20260808T005301Z-c8b13faa`,
  `run-20260805T144522Z-86ab8a14`, `run-20260808T024809Z-b56281da`.
- Tests: 10 new. Mutation-verified five ways — unwiring the backstop on the success path
  reds the named regression; removing it entirely reds 2; restoring the unconditional
  rejection wording reds the layer-1 test; forcing `dispatch_is_possible` to True reds 2;
  adding `"failed"` to `TRIAGE_ANSWERED` reds 2 including the derived guard that keeps
  that tuple honest. `test_the_answered_statuses_are_the_ones_the_driver_can_return`
  derives the set of statuses the driver really returns, so a typo in `TRIAGE_ANSWERED`
  cannot silently disable the backstop.
- One existing test corrected, not merely re-run:
  `test_the_driver_recognises_a_bus_delivery_at_its_entry_point` stubbed `_run_stage` to
  return `{"status": "ok"}` — a status no `return` in the driver produces. The backstop
  correctly read it as unanswered and paged. A double answering with a value production
  never emits is a double that tests a path production does not have.

### The env a Lambda needs is read from its handler, not copied by hand

`LAMBDAS["driver"]["env_keys"]` named six variables; `harness_driver/handler.py` reads
seven. The seventh is `ACTUALS_TABLE`, read by `handle_finops_tool` to record the cost
audit's `#finding#` rows — and the driver role has granted `PutItem` on that table since
the `CostActualsWrite` statement was written FOR this call. Permission present, code
present, variable absent: every finops turn that reached a terminal tool died on
`KeyError: 'ACTUALS_TABLE'`. Measured live 2026-08-01 (3x) and 2026-08-09 (3x — one per
Lambda async retry, each retry a fresh billed AgentCore turn re-deciding the same
period). `llmops-cost-actuals` holds **zero** `#finding#` rows for the entire life of the
system: not one variance the auditor found was ever recorded anywhere.

The gap survived eight days because nothing could see it. The crash needs a finops turn
to reach a terminal tool — once a day, inside an agent — so it surfaces as a dashboard
row that never appears, and the deploy reports success either way.

- `deploy/07_lambdas.py`: new `required_env_keys(src)` derives every `os.environ["KEY"]`
  read (no default = hard requirement) from the handler source; `env_keys_for(cfg)` unions
  that with the entry's `env_keys`, which is now **additive-only** — for defaulted reads
  we still want pinned per environment (`START_FN`, `PROJECT`). `OPTIONAL_ENV` exempts the
  four genuinely-defaulted knobs. `env_values` now RAISES on a required key it has no
  value for, instead of passing the rest and leaving the handler to crash a day later.
  Parsed rather than imported: a deploy script must not need the runtime's dependencies to
  know what the runtime needs.
- Tests: derived-vs-derived guard across all 7 Lambdas; the `ACTUALS_TABLE` regression
  pinned by name (so deleting the read cannot satisfy the guard); `env_values` refusal;
  and an `OPTIONAL_ENV` integrity check — an entry no handler defaults, or one that is
  ALSO read without a default, is the same bug hidden behind the exemption list.
  Mutation-verified: restoring the hand-maintained list reds three tests.

### A stage can outlive its runtime session; the driver now rolls before the 8h cap

AgentCore reclaims a runtime session at `maxLifetime` = 28800s (8h). That cap is
absolute: activity does not reset it and no setting raises it. The distillation stage
runs 8–12h in ONE deterministic session — 55+ tasks, turn after turn across
self-reinvokes — so it outlives the session it is speaking into, and the invoke that
crosses the line comes back as an ordinary runtime error. Nothing in the driver could
tell that from a real failure: it would spend the one stream-salvage retry, then the
re-ask budget, arguing with a session that can never answer again, and the stage would
die `MissingStageComplete` with the work still unfinished. The heartbeat + resurrector
pair (#67) cannot cover this one — the driver is alive and beating; it is the *session*
that expired, so every revival walks back into the same dead session id. The fix is to
stop letting the platform pick the moment.

- `orchestration/harness_driver/handler.py`: `session_id()` takes an `epoch`; epoch 0
  is byte-identical to the old id, so no past run's session ids move. `_run_stage`
  carries `_session_epoch` + `_session_started_at` in the continuation payload and, at
  the top of the turn loop, rolls to `…-e<N>` once the session passes
  `SESSION_ROLLOVER_S` = 25200s — leaving 3600s of margin for an 840s turn already in
  flight plus its handoff. Rolling happens ONLY between turns: mid-turn the session
  holds an unanswered `toolUse`.
- The pending message list does not travel to the new session. It is usually a
  `toolResult` answering a `toolUse` the fresh session never issued, which AgentCore
  rejects outright ("toolResult blocks … exceeds the number of toolUse blocks of
  previous turn" — the same shape found live on the console's dispatch path). The new
  session is re-seeded with the task payload plus a resume instruction pointing the
  agent at its own S3 outputs. That is not a workaround: S3 is where the state actually
  is, which is why the 2026-08-08 hand resurrection was lossless.
- The epoch is CARRIED, never derived from a clock at call time — the driver's
  self-reinvoke and the resurrector both rebuild the session id from the event, so a
  clock read would put a resurrection in a different session than the driver it
  replaced: two live sessions, one task token. The heartbeat's stamped payload carries
  the epoch too, pinned by test.
- `deploy/console/lambda_function.py`: rolled session ids are appended to the run row
  (`rolled_session_ids`) and `_recent_session_ids` unions them in. The console
  reconstructs ids from `(run, stage, task)` to aim batch evaluation at spans; a rolled
  id is not derivable from that tuple, so an unrecorded epoch is a session nobody ever
  scores — and rolled sessions are precisely the longest and most expensive ones. Both
  DynamoDB writes are best-effort: bookkeeping for a scoring convenience must never
  outrank keeping the stage alive.
- Tests: 9 new (`TestSessionRollover` + docs twin guard). Mutation-verified — disabling
  the roll reds 4 of them, and stubbing out the re-seed reds the `toolResult` pin;
  a derived guard fails if the console stops reading what the driver records.

### The signed plan now decides which models a run uses

`seed_manifest` built `models` as `{**DEFAULT_MODELS, **params.models}` — the signed plan
was never consulted. So a plan a human approved could name one teacher while the manifest
that run actually carried named another, and both ids sat in the same file. Live on run
68cfa9c8: `models.teacher = us.deepseek.r1-v1:0` (boilerplate) against
`plan.models.teacher = global.anthropic.claude-fable-5` (signed). The data-prep agent had
to notice the contradiction and pick the signed one **by judgment**, writing "top-level
manifest 'models' field is stale boilerplate" into the driver it generated.

It chose correctly. That it had to choose at all is the defect: model consent is
model-specific — approving a Fable-5 teacher at $0.05/1k output is not approving a
DeepSeek-R1 one — so this is the decision the signature exists to settle, handed back to
the model. An agent resolving it the other way spends real money on a teacher no human
approved, with a manifest that agrees with it.

- `orchestration/start_pipeline/handler.py`: new `_resolve_models(params, plan)`. The plan
  overrides `DEFAULT_MODELS`; `params.models` may fill roles the plan is silent about; a
  role where the two DISAGREE raises, naming the role and both model ids. Refusing rather
  than silently preferring the plan, because a disagreement here is never routine — it
  means the dispatch path and the approval path disagree about what was bought. One
  visible error costs less than an unapproved spend that looks authorized in every
  artifact afterward.
- Runs with no plan (scheduler, webhook — most runs) are unaffected: absent, empty, and
  `{"models": {}}` all still resolve to `DEFAULT_MODELS`, pinned by test.
- Tests: 5 new. Mutation-verified — disabling the conflict check reds the refusal test.
  Swapping the merge order deliberately changes nothing and the code says why: past the
  check, every shared role is proven equal, so the order is unobservable.
- Docs: ARCHITECTURE.md + `.zh-TW.md` state the authority rule where approvals are
  described as evidence.

### A stream can outlive the Lambda wall; the drain now watches the clock

boto's `read_timeout` (870s) bounds the gap BETWEEN chunks, not the stream's total
life. A reasoning model that trickles a chunk every few seconds can therefore stream
past the Lambda's 900s wall entirely inside one `_drain` call — where the driver's
between-turns `_out_of_time()` check can never look. Live: run 68cfa9c8's resumed
generate turn hit the wall mid-stream (`REPORT … Duration: 900000.00 ms … Status:
timeout`, 03:39:49Z); Lambda's async retry then replayed a continuation whose session
state no longer matched, and the stage failed `MissingStageComplete` with 51 tasks
still to run — the third failure class in this lineage, after the dropped reinvoke
(#67) and the prose turn-ends (#61/#62).

- `orchestration/harness_driver/handler.py`: `_drain` takes an `out_of_wall` check and
  abandons the stream with a distinct `LambdaDeadlineApproaching` marker when less
  than 45s of wall remains. The call site hands the turn to a fresh invocation via the
  existing `_self_reinvoke` (whole 900s available again; the harness turn finishes
  server-side meanwhile, and the salvage prompt asks the agent to restate its pending
  call). Deliberately NOT treated as a stream death: burning the one same-session
  salvage retry on a voluntary cut would leave a real death later in the stage
  unprotected — pinned by its own test.
- Tests: `TricklingStream` (never ends on its own) + the incident replay — red-first
  verified the old driver HANGS on it; and the no-burned-retry pin. The existing
  between-turns reinvoke test's fake clock moved from 10s to 500s remaining so it
  keeps testing the between-turns path rather than tripping the new in-stream cut.
### A dead driver now gets found in minutes, not by an operator at 2 a.m.

The driver hands each turn to its next invocation with an async self-invoke —
fire-and-forget by definition, and Lambda exercised the "forget": one dropped event
(AsyncEventsDropped=1, 2026-08-08 17:00Z) left run 68cfa9c8 dead for NINE HOURS at 4/55
tasks. Step Functions still RUNNING, token parked, money safe — and nothing anywhere
whose job it was to notice. An operator resurrected it by hand from the execution
history. The same silence swallows a driver that times out on its last turn, OOMs, or
crashes after the token check — and it is the reason AgentCore's 8-hour session
maxLifetime looked like a threat to 8–12h measurement stages: sessions may die (state
lives in S3, session ids are deterministic); what was missing was anything that
re-invokes afterward.

- `orchestration/harness_driver/handler.py`: every turn stamps `driver_beat_at` + the
  exact re-invoke payload (task token included) on the run row before doing anything.
  Best-effort: a throttled stamp must not kill the turn it announces.
- `orchestration/resurrector/handler.py` (new, 7th spine Lambda): every 15 minutes,
  re-invokes the driver for any run that is running AND beat-stale (20 min) AND holds
  no parked task token — a parked token is launch-and-release waiting on a SageMaker
  job by design; that wake belongs to resume_pipeline. The claim is a conditional
  update on the beat the sweep read, so two sweeps cannot double-resurrect one
  silence; past RESURRECTIONS_MAX (5) it emits EscalatedToHuman instead — a driver
  that dies every turn has a defect revival only re-runs. New informational event
  `DriverResurrected`.
- `deploy/07_lambdas.py`: the resurrector joins LAMBDAS; every function now gets an
  explicit EventInvokeConfig (2 retries, max age 300s) — the account default of SIX
  HOURS would redeliver a continuation long after the resurrector already acted.
- `deploy/08_triggers.py`: `llmops-resurrector-15min` schedule (rate(15 minutes),
  ENABLED by default); scheduler role may invoke it. `deploy/iam/lambda_roles.json`:
  its own least-privilege role (Scan+UpdateItem on runs, invoke driver, PutEvents).
- Tests (mutation-checked): the incident replay (stale beat → resurrection with the
  stamped payload), fresh-beat/terminal/pre-heartbeat rows untouched, the parked-token
  guard (deleting it turns the test red), conditional claim, cap escalation; driver
  side: every turn stamps the beat with the token included, and a throttled heartbeat
  does not kill a completing turn.

### The dispatch guard keyed on the task; the unit of idempotency is the acceptance

"One acceptance authorizes exactly one run" was enforced as "one task ever dispatches
once": the launch_run guard blocked whenever the task row carried ANY run_id. A
consultation thread whose run died gets a fresh signature for its continuation — and
that new acceptance, never honored, was refused with `already_dispatched` pointing at
the corpse. Found live within hours of shipping the continuation workflow: continuation
#5's acceptance (record `17fd4218…`) was refused because dead continuation #4's run_id
still sat on `task-70a558ec8da031d1`; the operator had to hand-clear the row in DynamoDB
to honor a signature the human had already given. The orchestrator's conduct on the
refusal is worth recording: it verified no new run existed, identified the two distinct
acceptance records, refused to blind-retry, and reported honestly.

- `deploy/console/lambda_function.py`: the guard now compares the latest approval's
  `record_sha256` against a new `dispatched_record` attribute written at dispatch time —
  blocked only when THIS acceptance already produced a run. Pre-fix rows (run_id present,
  `dispatched_record` absent) stay conservatively blocked: they cannot prove the new
  signature is new, and guessing is how a duplicate GPU run happens; an operator clears
  them deliberately.
- Tests: a fresh acceptance dispatches despite a dead predecessor's run_id (red against
  the old guard — verified by stashing the fix); an ambiguous pre-fix row stays blocked;
  the original one-acceptance-one-run test passes unchanged.

### A $0 capacity stop no longer spends the remediation budget

`resume_pipeline` treated `Stopped` and `Failed` identically (`TERMINAL_BAD`): a tracked
job stopped while Pending — a capacity race loser, or a quota wait given up on — fired
`TrainingJobFailed` and consumed one of the run's 3 remediation iterations, exactly as if
the code had crashed. But a job that never left Pending billed $0 and proved nothing;
spending remediation budget on it is spending budget on weather. The conductor's own
triage learnings called this out after the b56281da lineage: "capacity-stops ($0 billed)
should not consume the same remediation budget as code failures."

- `orchestration/resume_pipeline/handler.py`: `_is_capacity_stop` — Stopped AND
  `BillingSecondsUsed == 0` (the same economics the capacity-race guard runs on: Pending
  time is unbilled, so a stop that billed seconds was a judgment call on a *running* job
  and keeps the failure path). Up to 3 free relaunches per run (`capacity_retries`,
  incremented in the same DynamoDB write that clears the token — a separate write would
  open a crash window handing out uncounted relaunches); the 4th falls through to
  `TrainingJobFailed`.
- `orchestration/state_machine.asl.json`: `FinetuneLaunch` and `EvalGenerate` each gain a
  `CapacityStopped` Catch that re-enters the same state with `$.iteration` unchanged —
  no `IncrementIteration` on the path, pinned by a derived ASL test that covers any
  future launch state carrying the Catch.
- `pipeline/contracts/events.py`: `CAPACITY_STOPPED`, informational like
  `MODEL_FAILED_OVER` — the timeline's answer to "why did this state run twice".
- Tests (all red-first verified): zero-billed stop relaunches free with the counter
  riding the token-clear write; a billed stop stays a real failure; the 4th capacity
  stop stops being free; the Catch re-enters the same state.

### The eval stage gets launch-and-release; polling in-turn is what killed it

The eval agent's prompt said "there are no training jobs at your stage" — written when
eval meant judging a few dozen answers against a live endpoint. Reality caught up: student
inference for a 40-task holdout runs as a SageMaker training-type job that takes an hour,
and the agent's only legal way to span it was polling in-turn under a 10-minute cap. Long
polls are exactly where prose turn-ends happen; run b56281da died mid-poll with a healthy
job in flight and nobody listening for it to finish. Meanwhile the finetune stage has
never had this problem, because it has never been allowed to wait: `job_launched` parks
the token, EventBridge wakes the resume Lambda, a fresh session picks up. Eval now rides
the same rail.

- `agents/eval/harness.json`: gains the `job_launched` tool; the "evaluate" task either
  finishes small sets synchronously (stage_complete) or launches inference and releases
  (job_launched); a new "score" task picks up in a fresh session after the job completes
  — idempotent when evaluate already wrote the report. The stale no-training-jobs rule is
  replaced with finetune's launch-and-release wording.
- `orchestration/state_machine.asl.json`: new `EvalScore` state between `EvalGenerate`
  and `EvalGate` (waitForTaskToken, 86400s, Catch → EscalateFail). The happy path is now
  12 harness-task states; ARCHITECTURE (both languages) and PROJECT_STATE updated, and
  the derived docs-count guards forced every one of those edits.
- `orchestration/harness_driver/handler.py`: `handle_job_launched` documents that it is
  stage-generic (TRAINING_STARTED describes the SageMaker job kind; the run row's
  current_stage says whose it is); STAGE_EVENT_MAP maps ("eval","score") →
  MODEL_EVALUATED.
- `orchestration/resume_pipeline/handler.py`: a completed eval-stage job settles the
  token but does NOT announce MODEL_TRAINED — a batch-scoring completion on the timeline
  as a training would be a lie; EvalScore's stage_complete emits MODEL_EVALUATED moments
  later. Red-first verified.
- Tests: eval token parking (current_stage=eval), resume-without-MODEL_TRAINED, and the
  report-before-gate reachability test now derives the producer as the "score" task and
  additionally asserts evaluate reaches score.

### Every fleet prompt now states the turn-end invariant it was dying by

The driver's contract has always been mechanical — only a tool call ends a stage; prose
is `MissingStageComplete` — but the prompts only *suggested* it ("If work remains, call
checkpoint…"). Four runs in one week died on that gap, all the same fault: the specialist
did the work, then narrated instead of calling. The evidence that wording fixes it is a
controlled pair: run b56281da's plan params carried a hard turn-end rule, and every stage
whose agent read those params closed correctly; the eval stage, deep in SageMaker
polling where the soft sentence gives no order, ended three turns in prose and killed
the run.

- All 7 `agents/*/harness.json` prompts: a `TURN-END INVARIANT` bullet at the top of
  Rules, naming that harness's OWN terminal tools (finops has no stage_complete; its
  exits are publish_cost_report / update_rate_card / flag_variance), plus the write-first
  rule: artifacts land in S3 before the call that claims them. The orchestrator's
  invariant carves out consult mode — a turn that asks the customer a question properly
  ends in prose, and only a turn that owes a dispatch after "PLAN ACCEPTED" must end in
  launch_run — mirroring the console's own owes-dispatch re-ask logic.
- `tests/test_orchestration.py`: a derived guard
  (`test_every_harness_prompt_carries_the_turn_end_invariant_naming_its_own_terminal_tools`)
  reads each harness's tools[] and asserts both directions: every declared inline
  function is named in the invariant sentence, and no undeclared tool is (a stale name
  points the model at a function that returns 'unsupported'). Mutation-checked: deleting
  one tool name from one prompt turns it red.
- `agents/harness.json.template`: newly minted agents inherit the rule.

### The re-ask budget was a lifetime count; it is now consecutive

One counter, three dead runs. When a turn ends in prose instead of an inline-function
call, the driver re-asks up to 2 times, then fails the stage as `MissingStageComplete`.
That allowance was counted over the driver invocation's whole life and nothing ever gave
it back: an agent that slipped twice early, then worked correctly through checkpoints for
an hour, was executed for its third slip regardless of everything in between. Live:
run-20260808T024809Z-b56281da's eval agent burned both re-asks navigating a GPU capacity
race (stopping a $0 Pending job, relaunching on a fallback instance type — the right
calls, wrongly reported), recovered, and was then killed at 04:10Z by one more prose turn
while its third, healthy SageMaker job was mid-flight with nobody left to hear it finish.
Its two predecessors in the lineage (fb41420e, c8b13faa) died the same way.

- `orchestration/harness_driver/handler.py`: any serviced tool call — including a
  rejected `stage_complete` and an unknown tool name — resets `re_asks` to 0. What the
  budget counts is an agent that has stopped speaking protocol, not one that recovered.
  Two prose turns split by a self-reinvoke are still consecutive: the counter keeps
  riding the `_re_asks` continuation key.
- `tests/test_orchestration.py`: `test_a_serviced_tool_call_resets_the_re_ask_budget`
  (red against the old driver — verified by stashing the fix) and
  `test_re_ask_budget_survives_a_self_reinvoke` (pins the continuation carry, so a
  refactor cannot silently turn "consecutive" into "per-invocation"). The existing
  `test_missing_stage_complete_reasks_then_fails` passes unchanged — three consecutive
  prose turns still fail the stage.
- `docs/ARCHITECTURE.md` + zh-TW twin: the re-ask paragraph now says "consecutive" and
  why.

### The docs framed the agents as replacing engineers; they do not

Reader-facing wording only — no behavior, no numbers, no guard. The README's own section
heading read "Why the agents can **replace** an LLMOps engineer", `docs/CASE_STUDY.md`'s goal
statement opened with "Replace the human LLMOps engineer", and both the case study and the test
results priced the whole proof as "about one hour of a human engineer". That framing is both
off-putting and inaccurate about what this system actually does: it takes the 3 a.m. log-reading
and the sixth resubmission, and every judgment that matters still sits with the people who own
the system — which is why `escalate_human` is a first-class path rather than a fallback. Both
languages, swept together:

- The section heading is now "Why the agents can carry the routine work themselves", and both
  READMEs say so explicitly in a new closing paragraph: the aim is the toil, not the engineer.
- `docs/CASE_STUDY.md` / `.zh-TW.md` open with carrying one lifecycle end to end "without anyone
  having to stand by", and name the engineers as the ones called for the decisions.
- The cost comparison changed subject. "About one hour of a human engineer" became "less than
  this account spent in a single day on one idle endpoint nobody was watching ($36.36/day)" — in
  `docs/CASE_STUDY.md`, `docs/TEST_RESULTS.md`, both Chinese twins, and the intro page's cost
  card. It is the same order of magnitude and a better comparison: it measures the waste this
  repo exists to catch, against a number this repo already documents and guards
  (`tests/test_cost_model.py`), instead of measuring a person.
- Softer throughout where the meaning is unchanged: "human-free attempt" → "with nobody paged",
  "stops at the first 403 to ask a human" → "to ask for help", 升級人類 → 升級求助.

Left deliberately alone: `deploy/evidence/` and the phase table's "zero human intervention",
which are records of what a specific run did; `str.replace()` and prompt/event replacement in
code; and "no human user in the loop", which justifies a memory-strategy choice.

One guard did fire, and finding out why closed a hole in it.
`test_the_agent_count_readers_see_first_matches_the_fleet` anchors its zh-TW pattern on the
sentence I reworded, so it failed loudly instead of silently retiring — the assert-it-hit design
working as intended. Re-anchoring it exposed the other half: the era carve-out that lets
`docs/CASE_STUDY*.md` keep saying "six agents" accepted **any** number in a marked section.
Measured on merged main before touching it, `六 → 五` was green there — a section reading "five
agents … the v1 fleet … seven today" satisfied both existing checks, because neither asked whether
five was ever the fleet size. The accepted past count is now read from the sentence in
`deploy/evidence/VERIFICATION_phase5.md` that the section already cites as its reason, so a
former count nobody recorded fails, and a reworded evidence line fails loudly rather than
reverting the carve-out to accepting anything. Three negative controls (m138–m140), each
hand-driven to red on its own assertion: the past count drifting in each language, and the
evidence sentence being reworded out from under the derivation.

Counts refilled from the guards' own failure messages, not computed: negative controls
157 → 160 pairs over 141 mutations, both languages.

### Two guards from the previous change could pass having checked nothing

Follow-up to the change below, from reviewing it after it merged. Both holes are the same shape
and it is the shape this repo keeps finding: a guard that reads correctly, ran green, and was
satisfiable without examining its subject. Neither was reasoned about — each was driven to red
first, and the wording here says which mutation did it.

- **The ffprobe cross-check could compare zero clips.** The previous change moved this check off
  a duplicate walker in a deleted test file and onto `synth.mp3_duration`, the function that
  actually writes `durations.json` — the right move, and it left the loop's `if not p.exists():
  continue` in place with nothing counting how often it fired. `assert not bad` is satisfied by
  an empty `bad`, and `bad` is empty when nothing was compared, so a renamed audio directory or
  a changed language key turns "agrees with an independent decoder" into a green report over 0 of
  35 files. Measured, not feared: pointing `INTRO` at an empty directory passed the whole test.
  It now asserts `compared == len(LANGS) * len(SCENES)`. Worth naming why the missing-clip guard
  above it did not already cover this — it happens to fail first today, which is a property of
  *that* test, and a guard whose coverage depends on another guard's existence is one refactor
  from checking nothing.
- **"Re-measured" was left as prose.** The same change re-measured `161 tracked files, 35 binary`
  when the mp4 and poster were deleted, wrote the numbers into four comments across
  `tests/redaction_scan.py` and `tests/test_redaction_scan.py`, and derived none of them. Those
  numbers are load-bearing: they are the evidence for dropping the generic 12-digit heuristic on
  binaries, and for the empty-tree diff base covering the whole index. Re-measuring describes how
  a number was *produced*; it says nothing about whether it stays true. Committing one tracked
  file falsified all four sites with the full suite green — measured, not argued.
  `test_the_scanners_own_coverage_claims_match_the_repo` now derives both counts from
  `git ls-files` and `rs.is_binary`, one anchored pattern per site, and **asserts each pattern
  hit** so a reworded comment fails instead of silently retiring its own guard.
- **The past-tense carve-out, and why it is not a hole.** `"163 files became 161"` records that
  the file count moved while the 12-digit run statistics did not — which is the evidence that two
  numbers drifting together are not one number, so the line must be allowed to state a former
  count. It follows `HISTORICAL_FLEET_PATTERNS` in `tests/test_docs_claims.py`: the *current* half
  of such a phrase is still held to today's number, so the line recording the change cannot
  itself go stale, and a stale count cannot be legalised by writing "X became Y" around it.
- **Five negative controls (m133–m137), one per assertion.** Each hand-driven to red on its
  *named* assertion with the full failure body read: zero clips compared (0 of 35), a stale
  tracked count, a stale binary count, a reworded claim matching nothing, and the past-tense
  line's current half. The binary one is separate from the tracked one because of a finding
  worth keeping: driving it by adding a tracked binary file tripped the `tracked` assertion
  first and never reached `binary`, so a control written that way would have looked correct
  while proving only what another control already proved. A control that dies on an earlier
  assertion than the one it is meant to prove is indistinguishable from a working one from the
  outside.

### The film was in the repo twice, and the second copy cost every clone 10 MB

`docs/media/intro-en.mp4` is deleted, with its poster. The walkthrough plays from the
`user-attachments` upload GitHub hosts and renders as a real `<video>` element — the committed
copy was a *second* path to the same five minutes, 10,666,327 bytes of it, fetched by everyone
who clones this repo whether they ever watch it or not. The poster (`intro-poster.png`,
148,817 B) went with it: its only job was to be the clickable image whose target was the mp4,
so with the target gone it is a thumbnail linking to nothing. That second file was not named in
the request, and the reasoning for taking it is written here rather than left implicit.

**What this does not do: the blob is still in history.** It entered at `cdeea2d` and every
clone still fetches it out of the pack. Deleting a path from the tree removes it from the
*checkout*, not from the object store. Making the repo genuinely smaller means rewriting
history, which changes every commit id after `cdeea2d` and breaks every existing clone and
fork — a separate decision, deliberately not taken here. What this change buys is that the
next 10 MB does not arrive the same way.

- **Two single-language pull requests were opened first, and CI was right to fail both.** Each
  edited one README, and `hooks/pre-commit` §2 — which forces the EN/zh-TW pair into one commit
  — does not run in the web UI. But the pair was not the interesting failure. Both PRs showed
  `3 failed`, and only *one* of the three was the guard whose subject was the change: the other
  two were `test_the_walkthrough_section_states_no_number_it_does_not_derive` and
  `test_the_video_section_names_no_budget_amount`, which have nothing to do with a committed
  mp4. They failed because their **locator** was the thing being deleted:
  `_section_containing(text, "docs/media/intro-en.mp4")` asserts the needle is present in order
  to find the section around it, so deleting the file broke every guard anchored on it whatever
  its subject. The anchor is now `SECTION_ANCHOR = "user-attachments/assets/"` — the one string
  in the section whose presence is separately guaranteed, by the player guard. A heading would
  also work and was rejected: headings differ per language and get reworded by translation
  polish, which would fail these guards for a reason that is not their subject.
- **What was genuinely lost, stated rather than left for someone to find.** Six tests read the
  mp4's container: `moov` before `mdat` (faststart), an audio track as long as the video track
  (the narration actually muxed in), the coded frame size from the sample entry, square pixels,
  `yuv420p`, and the recorded length against the summed narration plus tail. None of those can
  be checked any more, because they were properties of an artifact this repo no longer has. The
  nine hand-built broken mp4s that drove them (4 KB truncation, `-an` at full length, a
  3-second audio track, `scale=640:360`, `setsar=59/32`, no `+faststart`, `-t 240`) are gone
  with them. `record_video.py` still measures its own output against the narration and fails the
  build past `--tolerance`, and that is now very nearly the only check on it — its docstring says
  so, and says not to widen the tolerance to make a run pass.
- **The frame-size guard was kept one step weaker, and the weakening is named.** It compared
  `.stage` in `page.template.html` against the recording's *coded* size — a real measurement of
  a real artifact, and the version that caught reading `tkhd` (display geometry) instead, where
  a 640×360 frame tagged SAR 59:32 reports width 1180 and a video with a third of the pixels
  would have passed. What survives compares `page.template.html` against `record_video.py`'s own
  `STAGE_W/STAGE_H` copy: two places the number is *written*. Changing one still fails (`m123`
  drives it). Changing both together passes, and no test can see it. Kept anyway, because a
  recorder pointed at a stage it does not match ships every diagram resampled, and nobody
  notices that by watching the film once.
- **The deleted ffprobe cross-check was pinning the wrong implementation, and its replacement is
  on the production code.** `test_the_mp3_fallback_agrees_with_ffprobe` compared ffprobe against
  `_mp3_seconds` — a *second copy* of the frame walker that lived only in the test file. The
  function that actually writes `durations.json`, `synth_narration.mp3_duration`, had never been
  compared to a decoder. It is now, over all 35 clips
  (`test_the_duration_measurement_agrees_with_ffprobe`), and why that matters is concrete: its
  sibling guard compares `durations.json` against `mp3_duration` — **both sides from the same
  function** — so a walker wrong by a constant factor writes a file that agrees with it
  perfectly. That walker *was* wrong exactly that way once, applying the MPEG-1
  samples-per-frame coefficient to Polly's MPEG-2 stream and reporting 11.7 s for 303.8 s of
  audio. Proven non-vacuous by halving the production walker: red, `walker 21.240s, ffprobe
  42.480s`. Worst real delta across the 35 clips: 0.004 s.
- **One guard exists only because of this deletion:** no `.mp4`/`.mov`/`.webm`/`.mkv`/`.avi` may
  be tracked, derived from `git ls-files` rather than a directory walk so the untracked recording
  `record_video.py` produces does not fail it. The negative-control runner cannot drive it — it
  mutates the text of one already-tracked file and cannot add an index entry — so it was driven
  by hand: `git add`ing an empty `docs/media/intro-en.mp4` turned it red on its named assertion,
  and `git rm --cached` restored the index. That is written into the control file, because a
  reader counting registered cases would otherwise conclude it has never been seen failing.
- **Seven controls retargeted, and the previous pass's own hole is closed.** PR #54 recorded that
  `m120` and `m121` both died on the guard's *first* assertion, leaving the assertion they were
  meant to prove never once observed failing. The four assertions of the player guard are now
  each driven individually: `m120` captions the line above the URL (fails
  `before.endswith("\n\n")`), `m121` adds a note on the line below (fails `after.startswith`),
  `m129` wraps it as a markdown link (fails the URL-exists assertion), `m130` points zh-TW at a
  different upload (fails the single-uuid assertion). `m121b` moved to
  `test_neither_readme_hand_writes_a_video_tag`, a test of its own now instead of an assertion
  riding inside one that required a committed file. `m131`/`m132` re-anchored on the headings.
  Each was hand-driven to red with the full failure body read, not the pass/fail count — a
  control that dies early is indistinguishable from a correct one if you only read the counts.
  152/152 pairs still pass; retargeting changed no count.
- Counts re-measured rather than adjusted: 163 tracked files → **161**, 37 binaries → **35**.
  The 12-digit-run statistics in the same comment (52 runs, 9 distinct, 0.15 s) were re-measured
  with the scanner's own regex and **did not move** — neither deleted binary contained one. Two
  numbers that had drifted together would not have been one number.

### The walkthrough section explained itself at more length than the film it introduced

Twenty-six of the section's thirty lines were about the section. A player, a poster, and
twenty-four lines arguing why the player is shaped the way it is: which of six embed forms
GitHub erases, why the inline copy is 8.9 MB and the committed one 10.7, what CRF each was
encoded at, what a reader whose language is not English should read instead. All of it true,
none of it what someone arriving at this repo is trying to find out, and a README is read by
agents as often as by people now — both of them pay for prose that answers a question nobody
asked. The section is a heading, the player, and the poster link; **the film is unchanged and
so is every byte in `docs/media/`.**
[Falsified by the entry above, one PR later: `docs/media/` no longer exists. Left standing
rather than edited, because what it recorded was true when written and rewriting it would hide
how short-lived "and nothing else changed" turned out to be. Read every claim below about the
poster link and the committed mp4 as describing a state that lasted one pull request.]

- **A guard that demands deleted prose is a guard someone deletes, so three were rewritten
  rather than left to fail.** They asserted the section stated "10.7 MB, CRF 26" beside the
  download link, that the download link existed at all, and that the paragraph naming 한국어
  pointed at `deploy/console/intro/`. Every one of those sentences is gone.
  - `test_the_readmes_state_the_committed_size_and_encoder_settings_correctly` is now
    `test_the_walkthrough_section_states_no_number_it_does_not_derive` — the same defect from
    the other side. The reason those two numbers were guarded has not gone anywhere: a size or
    a CRF typed into a README is measured on the day it is typed and reads as measured forever,
    and "how big is the download" is the obvious thing for the next person to add back. So the
    section's rule is now that it states neither, and anyone reintroducing one is told which
    number and why. Deleting the guard outright would have let the next helpful `10.7 MB` rot
    silently, which is exactly how the first one got there.
  - **Retuning the recorder's CRF now breaks no test, correctly, and that is a real loss worth
    naming.** `m132` used to mutate `"-crf", "26"` in `record_video.py` and catch a guard that
    *derived* it — the direction a hardcoded guard cannot see. With no README quoting the
    number, there is no claim left to falsify; the control was retargeted to the prose half it
    can still reach.
  - `test_both_readmes_reach_the_video_and_the_live_page` → `..._reach_the_committed_video`. The
    live page in its name was deleted from this repo months ago and the in-repo pointer that
    replaced it went with this trim; a guard named after something it no longer checks reads as
    checking it.
  - The poster link is now the **only** path from either README to the committed mp4. It used to
    be one of two, deliberately, so one careless rewrite could not orphan 10 MB. That redundancy
    is gone — the assertion is unchanged but nothing stands behind it.
- **Driving the controls, not reading them, is what caught the hole in this PR's own work.**
  `m120` and `m121` were both retargeted to delete a README's path to the file, and both then
  failed on the guard's *first* assertion (`"docs/media/intro-en.mp4" in text`) — leaving the
  poster-link assertion, the one now carrying the whole promise, never once observed failing.
  `m120` now downgrades the poster to a text link so the path survives and the poster assertion
  is the one that has to fire; `m121` keeps the outright deletion, on the other language,
  because the guard is parametrized over both files and m120 proves only one parameter.
- Eight controls retargeted in all (`m120` `m121` `m121b` `m122` `m129` `m130` `m131` `m132`),
  each hand-driven to red on its named assertion before being registered. `m122`'s budget-figure
  mutation moved to the section heading — the shortest-lived thing still guaranteed to be inside
  the section, and with no prose left, a caption on the heading is where a figure would come back.

### The live console's address was in the README, and the video embed never worked

Two defects on merged `main`, both reported by a reader looking at the repo page.

- **The front door of the live admin console shipped in a public README.** The rendered
  README, both `ARCHITECTURE` files, and two test files carried this account's API Gateway
  hostname — six sites. Reaching that page still needs a Cognito login, but it is where runs
  are launched and budgets are approved, and publishing its address invites the whole internet
  to knock. **Root cause: the redaction scanner had no rule for a URL.** Every rule it had was
  about identity — an access key, an account id, an ARN — so a hostname was not a leak any gate
  had an opinion about, and the only thing standing between it and the public was somebody
  noticing. `LIVE_ENDPOINT` now refuses any `*.execute-api.*.amazonaws.com` in text *or*
  binary, verified to flag five of the six sites and to excuse the sample origin
  `deploy/03_storage.py` prints in its own help text.
  - Its excuse list compares the **whole** captured id for equality. The first version searched
    for the example words as substrings, so an id merely *containing* an excused one walked
    through — found by writing the test, not by re-reading the regex.
  - That test then had the same class of defect one level up. It spelled out a sneaky id
    (`exampleandthenrealbits99`) that contains **no** entry of the excuse list at all, so a
    substring-matching scanner excused nothing and the assertion passed against the very defect
    it named — and so did the negative control written to break it. It now builds the id from
    the module's own tuple. Found by watching the control fail to fail.
  - The one test that hard-coded the real id now uses a stand-in. The resolver's behaviour never
    depended on which id came back, so nothing was ever bought by using the live one.
- **`<video>` in a README renders as nothing, and this was checkable all along.** The previous
  entry says the tag's fate "cannot be verified before pushing". It can:
  `POST /markdown` with `mode=gfm` returns exactly what the repo page will show. Measured
  there and against this repo's own rendered homepage — **zero `<video>` elements** — for a
  repo-relative path, a `raw.githubusercontent.com` URL, a `<source>` child and a release
  asset alike. GitHub builds a player only for files uploaded through its web UI, which a
  committed file can never be. So the guard was **requiring** a tag that provably does nothing,
  with a message claiming the reader could play it inline. The READMEs now lead with a clickable
  poster frame, and the guard asserts the tag is **absent** and that two independent links
  reach the mp4.
  - The `blob/…/*.mp4` page's *"can't show files that are this big"* is unrelated to size: a
    5.5 MB mp4 in an unrelated repo draws the identical refusal.
  - The guard strips code spans before matching, because both READMEs now explain in prose that
    `<video>` does not work — the naive version failed on its own documentation.
- **The walkthrough now does play inline, and the paragraph saying it never could is gone.**
  The entry above stops one measurement short. "GitHub strips `<video>`" is true; "so nothing
  plays inline on this page" does not follow, and both READMEs asserted it *as measured* — the
  same shape as every falsified doc claim this repo has had to sweep, written by the commit that
  was fixing one. There is exactly one form GitHub does promote: **a bare
  `user-attachments/assets/<uuid>` URL alone in its paragraph**, which is not a tag at all —
  the renderer recognises the link and builds the `<video controls>` itself. Verified through
  `POST /markdown` `mode=gfm` on the final section text: **one `<video>` element**, wrapped in
  `<details open>` with the upload's filename.
  - **The inline copy is not the committed bytes, and both READMEs now say so.** That URL only
    exists for a web-UI upload, and GitHub caps an attachment at 10 MB — the as-recorded file is
    **10,666,327 B (10.17 MiB)**, over by ~175 KB. So the upload is a CRF-27 re-encode,
    **8,864,103 B**, and the repo keeps the CRF-26 original. Not inferred from the rendered
    filename: the asset was downloaded whole and hashed — `e537416b…` matches the re-encode byte
    for byte and does **not** match the committed file — and it probes identical in every
    respect that matters (304.72 s, 1180×664, h264 + aac). Two files, one page, stated rather
    than glossed.
  - **The size in the prose is derived and the CRF is read from the recorder.** Both are numbers
    that outlive what they describe: "10.7 MB" was true when written, and a re-encode would
    leave it still reading as measured.
    `test_the_readmes_state_the_committed_size_and_encoder_settings_correctly` computes the size
    from the file and greps `-crf` out of `record_video.py`. It also asserts the committed file
    is still **over** 10 MB, because the READMEs' whole explanation of why there are two copies
    rests on that cap.
  - The size check reads the download link's own **paragraph**, not the section. The
    section-wide version passed a control that relabelled the download link with the re-encode's
    8.9 MB, because the paragraph above still mentions 10.7 MB — presence anywhere in a section
    is satisfied by the sentence *about* the discrepancy, which is not the sentence a reader
    reads as they click. Found by driving the control, not by review.
  - Four controls, hand-driven to red first: the URL "tidied" into `[text](url)`, the two
    READMEs pointing at different uploads, the download link relabelled with the wrong size, and
    the recorder's CRF retuned while both READMEs keep quoting the old one. `m120`'s anchor also
    had to be retargeted — the link it mutated was renamed from *Play* to *Download* — which its
    `count(old) == 1` assertion reported instead of silently mutating nothing.
- **A guard named "for every tracked file" was checking zero of them in CI.** Found in the CI
  log of the commit above, not by reading:
  `910 passed, 4 skipped` — one skip more than the three ffprobe cross-checks.
  `test_binary_classification_matches_git_for_every_tracked_file` diffed the index against
  **HEAD**, which on a clean checkout lists nothing, so it hit `pytest.skip("nothing staged")`.
  CI *is* a clean checkout, so the one machine that gates the merge never ran it — and its floor
  assertion (`assert checked`) was satisfied by a single file out of 163 anywhere else. It now
  diffs against git's empty tree (**163 files, 37 binary**, never empty) and asserts it covered
  `len(git ls-files)`, so the promise in its name is falsifiable.
- **This account's id was in the repo the whole time, as two adjacent halves — and our own
  scanner reported its own source clean.** Prompted by GitHub secret-scanning alert #1, which
  turned out to be about something else entirely (see below). The scanner and its test carried
  the twelve digits split across a `+`, on the theory that a value no regex matches is a value
  the repo does not contain. That theory is wrong in the only direction that matters: **the
  halves sat next to each other, in source order, in files GitHub renders.** A reader
  recombines them by eye in about a second, and so does one line of Python. What the splitting
  defeated was every automated scanner — this repo's included, which called
  `tests/redaction_scan.py` CLEAN. It hid the id from the machines that look for it and from no
  human at all, which is precisely backwards. GitHub raised no alert either: an account id is
  not a credential and has no detector.
  - **Fix: the scanner stores a salted, iterated digest and no digits.** Every 12-digit run in
    a blob is hashed and compared against `REAL_ACCOUNT_DIGESTS`, so the id is recognised —
    in binaries too — while no file contains it in any form. A *bare* sha256 would have moved
    the exposure rather than closed it: twelve digits is ~40 bits, and measured here CPython
    does **3.1M sha256/s**, putting the whole 1e12 space at ~4 days on this laptop and
    **~100 seconds on a GPU**. At 200k PBKDF2 rounds the same sweep is ~500 GPU-years, while
    scanning stays cheap because only digit runs are hashed: **52 runs, 9 distinct, 0.14 s for
    all 163 tracked files.**
  - **The guard was rewritten to look the way a human does.** The old one searched for one
    hand-spelled needle, which the split satisfied. It now collapses adjacent string literals
    *first* and then asks the digest, so a split into any number of pieces is caught, and it
    self-checks that the collapsing actually happens. The negative control for it could not
    find the id in git history by grepping for twelve digits — the defect restating itself —
    and had to join literals too.
  - **The round count is asserted, and the algorithm with it.** Pinning `_KDF_ROUNDS >= 100_000`
    alone is worthless: swapping the body for `sha256(salt + candidate)` leaves the constant
    sitting unused, produces identical findings, and keeps every other test green.
- **Alert #1 itself was a false positive, and is closed as one.** It flagged AWS's own published
  example access key — the `AKIA` + `IOSFODNN7EXAMPLE` body printed in the Signature V4 and CLI
  docs, not a credential — on commit `973d5c5c`, which is diverged from `main` and from every
  live branch (an ancestor of none of them). `main` already carries it split at the `AKIA`
  boundary. The real finding was the one no scanner reported. (Spelled here in two parts for the
  same reason the tests do: writing it whole makes this file trip the gate it is describing —
  which is exactly what the first draft of this entry did, and the scanner caught it.)

### The redaction gate: one rule set, and binaries are actually scanned

Found by staging the narration below — the first commit in this repo's history to add binary
files. Two defects, both live, in the pair of scanners that guard a public repo:

- **The commit hook text-grepped compressed audio and blocked on entropy.**
  `hooks/pre-commit` skipped files by extension *denylist* (`png|jpg|jpeg|gif|pdf|zip`), which
  does not include `.mp3`, so it ran the account-id regexes over 11.4 MB of MPEG frames. The
  generic bare-12-digit rule matched a twelve-long run of `0x33` bytes inside a frame — ASCII
  digits by coincidence, not an id — and refused the commit. (The exact byte run is pinned in
  `tests/test_redaction_scan.py`; quoting it here would make this file trip the very gate it
  describes, which is how the first draft of this entry got blocked.)
  **Measured: 1 of 35 clips.** Re-synthesise the narration and a *different* random
  subset blocks. An intermittent gate that fires on nothing is how people learn to pass
  `--no-verify`, after which it guards nothing at all.
- **CI never opened 44 of 157 tracked files.** `.github/workflows/redaction-check.yml` selected
  files by extension *allowlist* (`--include='*.py' --include='*.json' …`), so `frontend.html`,
  `page.template.html`, `test_intro_player.js` and three extensionless files were never
  scanned. Those are ordinary text and can carry an account id; a leak in one of them passed CI
  green. Scanned at the time of the fix — they were clean, so this closes an exposure rather
  than an incident.

The two scanners were hand-copied lists of the same five regexes with two different
file-selection schemes, which is how they drifted in opposite directions. They now share
`tests/redaction_scan.py`, and files are classified **by content** — a NUL byte in the first
8000 bytes, the heuristic `git` itself uses — rather than by a filename guess maintained by
hand. An extension list is wrong in both directions at once: it text-greps audio *and* skips
HTML.

Binaries are scanned with every high-signal rule — the two AWS access-key-id prefixes, the
secret-key assignment string, account-bearing ARNs — **plus this repo's own account id**; those
are structural enough to be safe on any byte stream. Only the generic any-12-digits
heuristic is dropped for them, because on binary data it is measurably noise — 0 structural
hits against 1 false positive across the same 11.4 MB. The residual gap is stated rather than
hidden: a bare 12-digit id that is neither this account's nor inside an ARN, embedded in a
binary, is not caught. `REAL_ACCOUNT_IDS` is the lever if a second account ever appears.

Both callers now fail closed when the scanner is absent — the same lesson as the SVG block,
which once skipped silently after its checker was renamed — and `rc=2` means *could not look*,
kept distinct so it can never be reported as *looked, and it is fine*.

Six negative controls, one per way this rots: binaries skipped entirely (the shape a
"simplification" of this fix takes); the real-account-id rule deleted while the structural
rules still fire, so a reviewer sees "yes, binaries are scanned"; the entropy rule re-applied
to binaries; CI drifting back to its own list; and two on the literals property below. **The
fourth was UNCAUGHT on its first run** — the guard grepped for two exact regex *spellings*, and
a re-inlined scanner written as `grep -rn AKIA` contains neither. It now asserts the *shape* —
no recursive content scan in either caller — with the spellings that must and must not match
pinned in a table, because a regex checked only against the one string its author had in mind
has unmeasured edges.

**Neither file spells a credential-shaped string as a literal any more**, and a guard enforces
it. This came out of the fix's own pull request being blocked. Both files are in the scanner's
`SELF_REFERENTIAL` list — they have to be, since these patterns are their subject matter — but
that exemption is knowledge local to *this* scanner. A session-level pre-PR hook scans the
branch diff with its own pattern list and no such notion, and it stopped the PR on five hits:
the `0x33` byte run, the three AWS-published accounts in the allowlist, AWS's own example
access key, a synthetic account and a placeholder ARN. **Not one was a secret.**

The tempting fix — teach that hook a per-file exemption — is a second scanner with its own
selection scheme, i.e. precisely the drift this entry is about, and it would have to be repeated
for every scanner that ever reads these files. So the values are assembled from parts
(`b"6833" + b"13688378"`) or rebuilt from their byte description (`bytes([0x33] * 10 + …)`)
instead. Nothing is weakened: the reconstructed run is asserted to be the identical twelve
bytes, and the allowlist tests parametrize straight off `ALLOWED` rather than a second copy of
it. The strings simply are not written down, which costs one `+` and needs no coordination with
anybody. The same reasoning already applied to this repo's real account id — now generalised
from one value to a shape.

Worth recording for whoever hits this next: this entry's own first draft was blocked by the gate
too, for naming the secret-key string and quoting the byte run. `CHANGELOG.md` was **not**
allowlisted — a file everyone edits is the last one you want to exempt — the prose was reworded
to describe the patterns instead.

### A narrated five-minute introduction, as the console's first tab

- **The console opened on a wiring diagram.** Architecture was the landing tab, which answers
  *how the system is built* to someone who has not yet been told *why it exists or what it
  cost*. A new **Introduction** tab sits left of Architecture and is now the default for a
  first-time visitor; a returning operator still lands on whatever tab they left.
- **Seven scenes, narrated, in five languages.** `GET /intro` serves one self-contained page
  (83 KB, 128 timed beats) built at deploy time by `deploy/console/intro/build_intro.py` from
  `page.template.html` + `narration.json` + `durations.json` + two architecture SVGs. Narration
  is 35 pre-synthesized Amazon Polly clips — English (default), 普通話, 粵語, 日本語, 한국어 —
  bundled in the Lambda zip and served by `GET /intro/audio/<lang>/<scene>.mp3`. The English
  narration measures 303.8s (5:04); the other four run longer, and the page rescales its beat
  timings and progress segments per language rather than assuming the English pacing.
- **Pre-synthesized rather than synthesized on demand.** `deploy/console/synth_narration.py` is
  a build step, so the console's IAM gains no Polly action, there is nothing to presign and
  nothing to expire, playback costs nothing per view, and the whole feature is testable
  offline. Screens are redrawn in CSS/SVG rather than captured, for the same reason: a
  screenshot of a console is stale the next time the console changes.
- **The page degrades to browser speech, per clip.** A missing clip is not an error — it is a
  robot voice, and nothing logs it. So the bundle is checked instead of trusted: `deploy.sh`
  hard-fails if any (language, scene) from `narration.json` is absent from the zip, if any clip
  is under 1 KiB, or if the package exceeds Lambda's 50 MB direct-upload limit, and
  `tests/test_intro_bundle.py` (40 tests) imports the handler out of a *reconstructed bundle*
  because the layout is the thing under test.
- **Two request-controlled segments go into a filename, so the route allowlists instead of
  sanitizing.** Cold start walks the bundle and records which (lang, scene) pairs exist; a pair
  that was not found is a 404 before any path is joined, so `..` is simply not a key. The audio
  response also sets `isBase64Encoded` — without it API Gateway sends the body as UTF-8 and
  every clip arrives corrupted under a *200*, which the page's own fallback then hides.
- **The default landing tab no longer reaches for Parameter Store.** `_resp()` built its CSP
  through `data_bucket()`, which resolves the S3 upload origin via SSM and does *not* cache a
  failed resolve — so the intro routes would have hit SSM on every request for an origin the
  page never fetches. They now pass `csp_upload=False`; **m111** and **m112** pin both
  directions, because "fixing" this by changing the default would strip the upload origin from
  the other 30 routes and block dataset uploads with a header that reads as an S3 permission
  error.
- **A negative control found the traversal test asserting one layer twice.** All eight payloads
  carried an extra separator or a wrong extension, so every one died at the shape check and the
  allowlist was never exercised — replacing the entire allowlist test with `if not lang or not
  scene` left it passing 8/8. The payloads are now split by which layer must catch them, with a
  second test asserting the split is still real, so a future tightening of the shape check
  cannot quietly retire the allowlist's coverage. **m109**, **m110** and **m113** cover the
  base64 envelope, the allowlist and `deploy.sh` dropping the audio copy.
- **The tab count in the diagram is now derived, not typed.** `gen_architecture_svg.py` read
  `8 tabs` from a literal; hand-editing the SVG would have satisfied the guard while leaving the
  generator to restore the wrong number on its next run. It now counts the nav buttons in
  `frontend.html`, like `HARNESS_N` and `LIMIT_USD` already did.

### The same walkthrough, playable from the README

A reader on GitHub sees a README, not a browser tab, and a link to a live console is something
they have to decide to click. `docs/media/intro-en.mp4` (10.7 MB, 5:04) is committed and
embedded in both READMEs so the five-minute walkthrough plays without leaving the page. The
live `/intro` page stays the canonical artifact — it is the one with all five narrations, and
the mp4 is English only, so both READMEs link it right beside the player.
[Superseded twice within this same unreleased block, and both times by something this entry did
not anticipate: the `<video>` markup GitHub strips never played at all, and the committed file
was then deleted as a second copy of a film GitHub was already hosting. The link to the live
page went earlier still — it was the address of the live admin console. Kept as written; the
top entry of this section is the current state.]

- **One clock, not two recordings.** `deploy/console/intro/record_video.py` plays the real page
  in a headless browser *in real time* and muxes the **same committed mp3s** the page just
  played. The page's clock **is** the audio element (`curTime()` returns `audio.currentTime`),
  so a 300 ms stall stalls the animation with the sound instead of sliding it ahead: sync is a
  property of using one clock, not of aligning two recordings afterwards. Rendering frame N at
  `t = N/fps` was rejected in writing — CSS transitions and SVG `@keyframes` animate on the
  document timeline, which does not advance during a screenshot, so every beat would pop
  instead of fade and the diagrams would be static.
- **Three separate offsets, measured, not absorbed into a tolerance.** Drift started at
  **+2.00s** and each cause was found rather than tuned away: an opening **lead-in** (recording
  begins at context creation, narration after load+click) trimmed with `-ss` after measuring to
  the first nonzero `currentTime`; a **deliberate 900 ms tail** (`TAIL_S`) held so the closing
  beat's `.5s` fade completes, named as a constant and *added to the expected length* rather
  than charged to drift; and a **trailing flush** as the browser finalises the file on context
  close, cut with `-t`. Final drift: **+0.00s**. Widening the tolerance twice would have been
  quicker, and a tolerance wide enough to hide a deliberate second is wide enough to hide an
  accidental one.
- **The recorder is a build step; the guard checks the result.** It needs Chromium and ffmpeg,
  and this suite is offline by construction, so `tests/test_intro_video.py` (11 tests) verifies
  the artifact instead of re-running the recorder: length against the summed narration clips,
  an audio track as long as the video, the authored stage size, `yuv420p`, `moov` before `mdat`,
  and that both READMEs reach the file two independent ways. [All six container checks were
  deleted with the file; the module is 7 tests and guards the presentation. See the top entry.]
- **Four guard defects found by breaking it, not by reading it.** Deliberately broken mp4s were
  built and driven past the guard, each one **with `ffprobe` removed from `PATH`**, because CI
  has no ffmpeg and a guard that only fails on a laptop gates nothing.
  - The truncated file made the faststart check raise `ValueError: b'mdat' is not in list` —
    red, but reporting a Python bug rather than the state of the file.
  - The no-ffprobe mp3 fallback assumed **MPEG-1** while Polly emits **MPEG-2** (24 kHz mono),
    so it returned **11.7s for 303.8s of audio**: a wrong answer in the right units, on the
    exact branch that runs in CI.
  - **A full-length silent film passed the entire module** — `7 passed, 3 skipped` — on the
    machine that gates merges. The audio-track and frame-size assertions sat behind
    `skipif(ffprobe)`, the same defect as the length check one test along. Both now read
    `hdlr` / `stsd` / `mdhd` out of `moov` directly, and a 3-second audio track against a
    304-second video fails on **duration** rather than passing on presence.
  - **The frame-size check read `tkhd`, which is *display* geometry.** A 640×360 frame tagged
    `SAR 59:32` reports width **1180** — the authored width exactly — so a video carrying a
    third of the pixels would have passed. It now reads the coded size from the sample entry and
    separately asserts the pixels are square, which also catches the reverse: right pixel count,
    stretched rendering.

  Each is cross-checked against ffprobe where ffprobe exists, and the length check reads `mvhd`
  from the container so it *runs* without ffmpeg rather than skipping into a green tick.
- **The authored stage size is derived from the page's own CSS**, not retyped and not read from
  `record_video.py` — the recorder keeps its own `STAGE_W`/`STAGE_H`, and a guard comparing the
  video against the *recorder's* number stays green while both drift away from the page the
  scenes are actually laid out in. **m123** re-authors `.stage` larger without re-recording, and
  the guard fails.
- **`"/intro" in text` was satisfied by `docs/media/intro-en.mp4`.** So the assertion that the
  READMEs still link the five-language live page could not fail while the video link existed.
  Found by **m121** deleting the live link and watching the test stay green; it now matches an
  absolute URL. **m120** and **m122** cover the video becoming unreachable and a budget figure
  reappearing beside the player.
- **No amount, in either language.** The walkthrough section says the reporting reference is
  set by each team and names no figure — checked by section boundary rather than a character
  window, so the guard does not police the pre-existing prose in the next section.

### The README never said what problem it solves

- **Both READMEs opened with an implementation, not a problem.** The first sentence was "An
  end-to-end LLMOps platform, run autonomously by AWS Bedrock AgentCore Harnesses", and by
  line 7 a newcomer was reading about conductors, DeepSeek-R1, QLoRA and SageMaker endpoints —
  three unfamiliar words deep before learning whose pain any of it removes. Someone deciding
  whether this repo is relevant to them got no answer above the fold.
- **The evidence for the pain was already in the repo, filed under other headings.** Six
  rounds of dependency and CUDA-OOM failures to get one QLoRA job to `Completed`
  (`docs/CASE_STUDY.md`); an endpoint billing $36.36/day at 0 invocations and 0.0% GPU over
  90 days with no owner tag (`PROJECT_STATE.md`); quality gates that scored this project's own
  model 0/16 and were not talked past; ≈ $12–15 for the whole proven lifecycle. A new
  `## The problem this solves` section in both languages states the pain, why an assistant
  that only suggests commands does not close it, and what runs instead — every number cited to
  the file that measured it, no new claims invented. Depth stays in the case study, linked.
- **The section describes the reporting mechanism without naming an amount.** The configurable
  reference that names overspend is a per-team setting, so quoting this platform's own figure
  as though it were the design would misrepresent it.

### "Six agents" outlived the six-agent fleet

- **Both CASE_STUDY variants say "six agents"; `agents/*/harness.json` holds seven.** The
  FinOps auditor was added after Phase 6, and nothing noticed for the entire life of the
  seventh harness — the same drift-by-addition that already produced a stale Lambda count and
  a stale ASL state count. A number that was once measured looks measured forever.
- **Scoped rather than renumbered.** Changing it to seven would contradict the evidence file
  the document cites (`VERIFICATION_phase5.md`: "All six harnesses currently run Opus 5") and
  claim the auditor took part in a build it was absent from. Both variants now say six *and*
  say it is the v1 fleet, naming today's count alongside.
- **The count on the first screen had no guard at all** — which is precisely why "six" could
  stand indefinitely. `test_the_agent_count_readers_see_first_matches_the_fleet` derives the
  fleet size from `agents/*/harness.json`, requires each README's claim to match, and treats a
  claim deleted or reworded away as a failure rather than a pass. A smaller historical count is
  allowed only in a `##` section that marks it as past *and* names the current count, so the
  scoping note cannot itself become the next stale number. **m106** (English prose drifts),
  **m107** (the zh-TW twin drifts) and **m108** (the v1 scoping deleted, leaving a bare stale
  count) each fail it. A guard hardcoding 7 would have missed the direction that matters — an
  eighth harness landing while the prose stands still — verified by hand here, since this
  runner mutates existing files and cannot create one.

### The readiness panel counted nine questions and said six

- **`task_readiness`'s docstring described the panel as showing "which of the *six* data
  questions nobody has answered yet"; `DATA_READINESS_FIELDS` holds *nine*.** The list grew to
  nine in the commit that derived it from the orchestrator's consult prompt — the fix for a
  panel that was missing `datasheet.provenance` and `readiness_report_uri` — and the sentence
  explaining the panel stayed at six. Nothing was functionally wrong: every readiness test
  measures the tuple, so all nine questions were asked, answered and counted correctly in the
  API response. What was wrong is the explanation, which is what a reader believes when
  deciding whether the panel covers what they care about.
- **Found while writing an operator runbook, and it had already spread.** The count was copied
  out of this docstring into two operator-facing documents before anyone checked it against
  the tuple — a false claim in a docstring propagates at the speed people quote it.
- **The guard derives the number rather than restating it, and fails from both sides.** The
  count in the tuple was guarded (`test_readiness_names_every_field_the_consult_protocol_asks_for`);
  the count in the prose was not, so the prose is the copy that drifted. The new guard reads
  `len(DATA_READINESS_FIELDS)`, maps it to its number word, and requires the docstring's count
  sentence to name exactly that one — so **m104** (prose back to "six") and **m105** (tuple
  shrinks to eight, prose left at nine) both fail it. A guard that hardcoded "nine" would
  catch m104 and sail past m105, which is the difference between checking a claim and
  restating it. It anchors on the single line containing "data questions" rather than
  searching the module, because "nine" appears in another test's own prose; and it rejects
  digits as well as words, since `6` would slip past a word search.
- **Same shape as this panel's earlier defect.** ARCHITECTURE.md already records the version
  where the *guard* restated seven paths against a prompt specifying nine. Derive-don't-restate
  now covers the prose as well as the list.

### A limit without its mode is the more misleading half

- **`GET /api/cost-overview` reported the two dollar limits and not whether either is
  enforced.** `limits` carried `single_usd`, `cumulative_usd` and `approver_group`;
  `budget_mode` existed on the `gate` object but not here — so the one surface a human reads
  was the surface missing it. Found while verifying the $2000 → $20,000 raise live: the
  overview came back with the new number and `budget_mode: None`. `limits` now carries
  `budget_mode` and a derived `enforced`, computed with the same predicate the gate itself
  uses (`BUDGET_MODE == "blocking"`), so the label cannot disagree with the branch.
- **That fix went in on one of the two `limits` payloads, and shipped that way.** The
  console publishes a dict named `limits` from both `cost_estimates` and `cost_overview`;
  only the first got the mode. Reading the deployed API back is what found it —
  `/api/cost-estimates` answered with `budget_mode`, `/api/cost-overview` answered with two
  bare numbers, and every test above stayed green because each names the estimates endpoint.
  The entry above even said a correction "holds until the next person adds a limits
  consumer"; the second consumer already existed. Both payloads now carry it, and the guard
  that would have caught it **derives the list from the source** — every dict literal
  assigned to a `limits` key must state its mode — rather than naming the payload somebody
  remembered. m103 reproduces the shipped state: first payload honest, second one bare.
- **The Cost KPI card read as a stop sign.** Its own words were *"limit $20,000 per run /
  $20,000 cumulative"*, with nothing saying that in `advisory` — the deployed default — an
  over-budget run is named, priced, and then launched anyway. It now renders
  *"reference … · ADVISORY — an over-budget run is reported, then launched anyway"* in amber,
  or *"ENFORCED — an over-budget run is held for an approver"* in green. Verified live at the
  new reference: n=260,000 straddles it ($19,775 expected, $21,056 worst case) and returns
  `over_budget_usd=1056.30` with `status=approved` — the run launched.
- **Guarded in both directions, and against behaviour rather than against itself.** A field
  hardcoded `False` satisfies an advisory-only test forever and mislabels every blocking
  deployment; one hardcoded `True` is the original defect with extra steps. So `enforced` is
  asserted equal to what an over-budget launch *does* — held with 409, or invoked — once per
  mode. The two modes are checked by two single-fixture tests calling one helper, not by one
  test requesting both fixtures: the `blocking` fixture monkeypatches `BUDGET_MODE` on the
  same module object `wired` hands out, so a test asking for both gets blocking twice and
  passes while comparing nothing. That version was green in the wrong direction first.

### Stage timeouts: a day for real work, an hour for anything holding a GPU

- **The six states that wait on real agent work now carry `TimeoutSeconds: 86400`** — a full
  day, raised from 7200/21600 on the platform owner's instruction. `TimeoutSeconds` is the
  only real ceiling on a stage: the driver Lambda's 900 s does **not** bound it, because the
  driver self-reinvokes across invocations via `_continuation` — but the `.waitForTaskToken`
  token it holds only lives for `TimeoutSeconds`. A 480-teacher-call generation run does not
  fit in two hours, and the 2026-08-01 run was cut off by exactly that, mid-work, with the
  work already paid for.
- **The seven bookkeeping states deliberately did *not* move.** `Teardown` is what deletes the
  endpoint and `MonitorHealth`/`MonitorReport` sit on the only path to it: a wedged `Teardown`
  at 86400 s holds an `ml.g5.2xlarge` InService for a day at $1.515/hr — the exact shape of
  the 843-day, 0-invocation orphan this project already paid for and deleted on 2026-08-02.
  Raising every state with one `sed` would have been a cost regression dressed as a
  reliability fix. The split is now asserted, and the guard **fails on an unclassified new
  state** rather than defaulting it into either bucket, because defaulting is how a cleanup
  stage would silently inherit a 24-hour ceiling.
- **`HeartbeatSeconds: 18000` on `FinetuneLaunch` and `RemediateFinetune` was a shorter
  deadline wearing a liveness signal's name, and it was live for weeks.** Step Functions
  fails a state with `States.Timeout` if the heartbeat interval elapses without a
  `SendTaskHeartbeat` — and **nothing in this platform has ever called it**, though the IAM
  role grants `states:SendTaskHeartbeat`. So the first heartbeat never arrived and both
  states really died at **18000 s while their ASL said 21600**. Every surface agreed with the
  ASL: the console's hover card rendered a `heartbeat 18000s` row, which reads as *we monitor
  liveness*, not as *this stage has a 5-hour cap you cannot see anywhere*. It surfaced only
  because the raise to 86400 would have left those two dying at 5 hours while all six
  siblings ran a day. Both fields removed, along with the console reader and hover row that
  displayed them — and the existing
  `test_every_field_the_hover_card_renders_is_supplied_by_the_api` is what caught the
  now-unproducible field, so the dead UI could not linger. A heartbeat may come back, but
  only **with** a sender: that is what the new guard permits and what it refuses.
- Two hover-card tests pinned the literal `7200` while being about something else entirely
  (that a failed AgentCore lookup must not drop the ASL half of the card). Both now derive
  the value from the ASL: a literal in a test whose subject is not that literal is a tripwire
  on the wrong wire, and rewriting it to `86400` would just re-arm it for next time. The
  card's timeout row also renders hours above an hour — "1440 min" is not a duration anyone
  reads as a day.

**785 pytest**, **103/103 negative controls** (89 mutations). Four new controls: `Teardown`
inheriting the day, `DataPrepGenerate` reverting to 7200 (the shape a merge conflict resolves
wrongly by default), a new timed state shipping unclassified, and a heartbeat interval
returning without a sender.

### The ASL deploy has to prove it landed, not report that it was sent

- **`deploy_state_machine` now reads the definition back and refuses to call the deploy done
  until the live machine matches the ASL in this tree.** It used to return
  `action: "updated"` on the strength of `update_state_machine` returning 200 — which says
  the call was accepted, and nothing about what the machine will run. On 2026-08-03 the live
  definition turned out to be **a state behind**: `EvalGenerate` was entirely absent, though
  it merged on 2026-08-02 in `7940af8` as the whole point of #57, along with six stale
  timeouts, both senderless heartbeats, and `FinetuneAnalyze` still pointing at `EvalGate`.
  A human found that by reading the live definition by hand. Nothing in the repo compared
  the two, so nothing could have found it.
- **This repo had already paid for that belief once.** `update_function_configuration` was
  called without `Role` for months while every run reported "updated" and each function kept
  its birth role — the defect
  `test_a_role_change_reaches_an_existing_function_not_only_a_new_one` exists for. The same
  file's `live_bus_translator_gap` even argues the point in its own docstring, and **blocks**
  the deploy on it, for bus rules. The state machine definition never got the same treatment.
- **The comparison is semantic and it names states, not bytes.** Step Functions happens to
  return the definition verbatim today (measured: 26742 bytes both sides, parsed-identical),
  but a formatting-only difference is not a deploy failure, and a check that calls it one gets
  switched off by the third person it wakes. So the drift walk parses both sides and reports
  "`EvalGenerate` is absent live" rather than a 26 KB diff — and when equality fails and
  neither walk can localise it, it says **that**, because reporting clean on the one case it
  cannot explain is the exact failure it exists to prevent.
- **It polls rather than reading once.** AWS documents `UpdateStateMachine` as eventually
  consistent — executions started immediately afterwards may still use the previous
  definition. Five reads, backing off 1/2/4/8 s. A guard that cries wolf gets deleted, which
  is the same eventual consistency that bit the push tool's ref read in #35.
- A read-back that cannot run reports `definition_confirmed: false` with the reason, never a
  confirmation. Not being able to check is not the same as having checked.

**795 pytest**, **107/107 negative controls** (93 mutations). Eleven new tests and four new
controls: the read-back deleted from the deploy path, the "cannot localise" backstop returning
clean, the eventual-consistency wait removed so a lagging read reads as drift, and an
unreachable read claiming confirmed. The backstop's control went **uncaught** on the first
run — the test fed it a difference the top-level walk does localise. Its real input is a key
present-with-`null` on one side and absent on the other: unequal dicts, and every `.get()`
comparison agreeing, which is the one shape both walks are blind to. A control that goes
uncaught means the test named the wrong subject, not that the control needs weakening.

### A harness deploy has to prove it landed too — READY is not "serving what you sent"

- **`05_harnesses.py` now reads each harness config back and refuses to call the deploy done
  until the live config matches this tree.** It returned `action: "updated"`, `status:
  "READY"` on the strength of `update_harness` returning 200 and `wait_ready` seeing READY.
  Neither says anything about *what the harness answers with*, and READY is the more
  dangerous of the two, because a harness serving a stale prompt is READY the entire time
  it is wrong.
- **Two harnesses were live-drifted when the guard was first run, and only one was known.**
  `llmops_finops` still quoted the falsified orphan rate — the $18 daily figure that was half
  the real one — although #41 had put `$36.36/day` on main two days earlier. The one nobody knew about: **`llmops_data_prep` was
  932 characters behind main**, missing the entire Macie paragraph from #63 — so the
  data-prep agent was still being told it had no way to report a real classification result,
  months of that work deployed to S3 and never to the harness. The other five reported clean.
- **The comparison is containment, and that is a measurement rather than a preference.** On a
  perfectly synced harness `environment` still differs: the deploy sends
  `networkConfiguration` + `lifecycleConfiguration`, and the service returns those *plus* the
  `agentRuntimeArn`/`Name`/`Id` it assigned. Strict equality would report drift on every
  correct deploy of all seven harnesses, forever — and a check that fails a correct deploy is
  one the third person it wakes switches off, taking the real check with it. So the question
  asked is: is every value we sent present and equal live? Keys the service added are its own
  business. Verified against all seven live harnesses before shipping.
- Drift is reported by **dotted field path** (`environment.agentCoreRuntimeEnvironment.
  lifecycleConfiguration: sent, but ABSENT live`), and a long value by length plus first
  divergence rather than pasted twice — the finops prompt is 6539 characters, and dumping
  both sides is how the one line that mattered scrolls off the screen. "Sent but absent" is
  kept distinct from "differs": different causes, different fixes.
- **The read-back runs before `warm()`.** Warming first spends up to six real model turns
  making a harness that serves the *wrong* prompt fast to reach, and prints a reassuring
  "warmed" line above the failure.
- The update payload and the read-back's field list are now one name (`UPDATED_FIELDS`). Two
  lists that agree today drift later, and a field added to the send but not the check can
  fail to land silently — this defect reintroduced one field at a time. `memory` is
  deliberately excluded: it belongs to `04_wire_memory.py`, and reporting another owner's
  field would make every run look broken.

### A duplicate test name deletes the earlier test, and no count notices

- **`test_no_test_function_name_is_defined_twice_in_a_file`.** Python keeps the later `def`,
  so the first is never collected and never runs. Nothing in this repo noticed: the
  collection total still goes *up*, so the count guard is satisfied, and the suite stays
  green because the surviving test passes. Found by writing the harness guards above — a new
  test reused the exact name of the ASL read-back test from #80, in the same file. That test
  silently left the suite, and the negative control verifying it (`m93`) named the shadowed
  node id: it would have gone on printing PASS while measuring a different test's failure.
- Its own first control went **UNCAUGHT**, which is the same lesson in the same commit: the
  guard asserted only "this repo has no duplicates", and that passes whether the detection
  works or not while the tree is clean. Split into a detector checkable against input that
  *has* the defect, plus a separate repo-wide sweep. A control that goes uncaught means the
  test named the wrong subject — never that the mutation needs weakening.

**808 pytest**, **113/113 negative controls** (99 mutations). Six new controls: the harness
read-back deleted, equality demanded where the service adds keys, warming before confirming,
the field list allowed to drift from the payload, an unreachable read claiming confirmed, and
a shadowed duplicate going unreported.

## [1.2.0] — 2026-08-02

Twenty-one merged PRs (#17–#38; #24 closed unmerged). The pattern across almost all of them
is one kind of defect: **a component that existed, was deployed, was tested, and was never
reached.** A dispatch that no state ever called, a bus rule set that was empty, a success path
that had never executed, a reply channel with nothing listening. Each looked healthy from
every dashboard, because the half that was written was the half that worked.

### A consultation the customer can actually finish

Before this release a customer could not hand us a dataset. The Tasks tab could show a run
but had no way to start one from a goal, and there was no upload path at all.

- **Goal-driven consult entry** (#17) — a natural-language goal enters the orchestrator in
  consult mode and comes back as a costed plan; acceptance is **KMS-signed**, and
  `service_launch_run` verifies the signature and the plan hash before `start-pipeline` sees
  anything. An approval that cannot be verified is not an approval.
- **Presigned dataset upload** (#22) — plus bucket CORS, the console's `PutObject` IAM, and an
  **httpOnly refresh cookie**, because the old refresh path signed the customer out in the
  middle of the upload they were signed in to perform.
- **One thread, not a form** (#23, #31) — the tab became a single Claude-Code-style
  conversation with a drop zone. Parked directive rows had been pushing the newest real events
  off the timeline, so the customer watched a stale run.
- **The identity in the hover card is resolved, not guessed** (#18), and the teacher-token
  estimate was recalibrated — the old figure made the plan's own caps arithmetically
  infeasible, so every plan it produced was unexecutable.
- **The audit copy stops erasing, truncating, and gating itself** (#36's branch) — a failed
  transcript read was treated as "no file yet" and wiped the history; a failed audit write
  stranded a signed acceptance it had no authority to gate.

### The pipeline dispatches every stage it claims to dispatch

Three stages were configured, documented, and never dispatched. All three were found by
asking what actually calls the thing, not by reading what declares it.

- **The eval gate read a report nothing wrote** (#33) — `evaluate` was never dispatched, so the
  gate consumed a file that did not exist.
- **The `llmops-pipeline` bus carried ZERO rules** (#35) — `EscalatedToHuman` was published to a
  bus with no subscribers, so every escalation was emitted into nothing. Now routed to the
  conductor for triage, with `page_human` serviced on the driver path as triage's only
  above-authority exit.
- **`llmops_monitor` had no task dispatched anywhere** (#36) — a runtime deployed and wired into
  the state machine that nothing ever asked to do work.
- **The SUCCESS path had never run** (#19's window) — nothing wrote `status=completed`, so every
  successful run was a zombie record. A happy path that has never executed is not a path.
- **Per-run report keys** (#19) — one shared key meant each run overwrote the last one's report.

### FinOps and governance

- **The orphan endpoint costs $36.36/day, not the $18 six files claimed** (#37). The $18 was the
  first sweep's *guess* — that sweep could not call `DescribeEndpoint` and said so in its own
  report — and it understated by 2×, which is the magnitude an owner can dismiss on the merits.
  Now derived from `describe_endpoint_config` (ml.g5.2xlarge ×1) against the documented hourly
  rate. The endpoint had been InService for 843 days (2024-04-11 → 2026-08-02) with 0
  invocations and 0.0% GPU utilization; deleted 2026-08-02 under explicit authorization.
  **The sweep that corrected it missed the one file that matters most**: the guard was anchored
  to the three files that sweep edited, so `agents/finops/harness.json` kept telling the auditor
  the orphan cost half that rate — inside the very rule about never publishing an assumed number as a
  measured one. A prompt is the worst place for a falsified figure: nobody opens a doc mid-audit,
  and the agent re-reads its prompt on every invocation. The guard now scans **every**
  `agents/*/harness.json` rather than naming the file that was wrong, since naming the file is how
  the hole got there, and it requires the measured rate to be **present** — an absence-only check
  passes on a deleted sentence, which would have left the attribute-by-resource rule with no
  concrete example.
- **`bedrock-monthly-dev` is stated in both directions** (#37) — its `Service: ["Amazon Bedrock"]`
  filter is simultaneously what kept the $1000 guardrail meaningful and what made it blind. No
  account-level control would ever have flagged that endpoint; the whole-account monitor sweep
  is what found it. `describe_budget_actions_for_budget` returns **0 actions**: it notifies, it
  does not enforce.
- **The budget became advisory but stayed spoken aloud** (#21) — `BUDGET_MODE=advisory` reports
  the overage in `start_run`'s response rather than blocking. Removing the number entirely would
  have deleted the only line that says a run is more expensive than its plan.
- **The reference is $20,000, raised from $2,000, and the deploy now sets it** — the platform
  owner's instruction: this is the project's own design-and-test platform, not a customer's
  production account, and the entire test-proven record cost ~$12–15. A reference low enough to
  be crossed by ordinary work gets clicked past. Two things the raise exposed, both worse than
  the number being low:
  - `deploy.sh` set **neither** limit, so the live function reported `APPROVAL_LIMIT_USD: null`
    and fell back to the console's own literal — which happened to agree. Nothing was wrong and
    nothing could have told us when it stopped agreeing. Both are now derived from
    `cost_model.DEFAULT_*_LIMIT_USD`, and the console's fallback copy is pinned equal to the
    canonical one by a test, because two copies of a number with nothing comparing them is
    exactly how one falsified figure survived in four files at once.
  - **The straddle fixtures stopped straddling.** Nine budget tests were built on a literal
    2,000,000 rows priced at $1,268 expected / $3,804 worst case — both under $20,000, so the
    tests would have gone green while never engaging the budget check at all. Their own
    docstrings named this hazard; a limit change is that hazard arriving on purpose. The plan is
    now derived from the reference and the straddle is asserted on every use.
- **A real PII scan, or an honest absence** (#36) — Macie `llmops-customer-data-pii`, daily
  SCHEDULED over `customer-data/`. Until it existed the audit's answer to "did anything scan
  this data" was silence, which reads as yes.
- **`budgets:ViewBudget`** (#25) — the action `DescribeBudgets` actually authorizes against, not
  the one its name suggests.
- **The rate card is priced from the file callers are told to read** (#20) — the document shape
  the fetcher produced and the shape the pricer expected were different, so a card that
  refreshed successfully priced nothing.

### Latency: 2–5 uncached round-trips per turn, whole turn buffered

- **Inject the rate card instead of making the agent fetch it** (#26) — a tool call per turn for
  data that fits in the prompt.
- **Stream the reply** (#27) — the whole turn had been buffered before the first character
  reached the browser, so a correct answer looked like a hang.
- **READY does not mean warm** (#28) — a harness reports READY before its first session pays
  the cold-start cost, so deploy now warms it. The effort knob was never the lever.
- **Log the round-trips a turn really made** (#29) — the previous log line was structurally
  incapable of showing the real count, so the latency work had no measurement to stand on.

### Release engineering: the push tool, and a PR that shipped to nowhere

Direct `git push` is hook-blocked here, so `tools/push_via_api.py` is the only path to the
remote — and it was silently corrupting history in four distinct ways, each found by comparing
the pushed tree to the local one rather than by trusting a green push.

- **Squashed commits** (#29), **commits replayed on every subsequent push** (#30), **an
  eventually-consistent ref read** (#19), and **merges flattened in two places at once** (#34).
- **`deploy/07_lambdas.py --only` now means only** (#36's branch) — it both over-deployed past
  its argument and blocked the narrow deploy it was added to enable.
- **All 19 skill mounts moved git → s3, resolved at deploy time** (#36's branch) — a git skill
  source has no branch field, so every deployed harness read the skills repo's default branch
  and a push there silently changed production. `ensure_skills()` landed in
  `deploy/03_storage.py` **before** any source was switched, because a bad skill source is
  accepted by `UpdateHarness`, reports READY, and then fails every session at start.
- **The capacity race guard finally reached main** (#38) — #10 merged it into a non-main base,
  so its 10 shell assertions sat outside CI for days while every badge was green. Reading the
  `validate` log, not the badge, is what found it.
- **Diagrams and docs corrected against the running system** (#30, #32) — including the audit
  plane, the escalation path, and the skill-source language.

### Tests

- **The control runner leaked its mutation when signalled, and a `try/finally` was why nobody
  noticed.** The restore has always been inside a `finally`, so the runner read as safe.
  SIGTERM's default disposition terminates the process without unwinding — no `finally`, no
  `atexit` — so killing it at a tool timeout left `m52`'s edit to `deploy/03_storage.py` in the
  working tree, found afterwards by `git status` and nothing else. A full run takes ~3 minutes,
  which makes being killed partway the ordinary case, not the exceptional one. The damage is
  not the dirty file: it is the **next** run, which mutates an already-mutated file and then
  reports PASS about code nobody wrote. Two defences, because neither covers the other's gap —
  handlers that raise so the existing `finally` fires, and a journal written **before** the
  mutation so `SIGKILL`, which no handler may intercept, still leaves the original recoverable
  and the next start repairs the tree before trusting it. Verified against a real reproduction
  in both directions: SIGTERM restored the file, SIGKILL leaked it, and the next start printed
  `RECOVERED` and undid it.

**785 pytest** (from 274 at v1.0.0), **103/103 negative controls** (89 mutations, 103
(guard, mutation) pairs), **10/10 shell assertions**, three SVGs geometrically CLEAN against
six checks. Offline by construction: `tests/conftest.py` strips AWS credentials and refuses
non-loopback sockets, so a credentialed laptop cannot turn a test that hits production into a
passing test.

Four of this release's guards were **fixed by their own negative controls** — including one
that certified as clean the exact defect it was written to forbid, and one that was wrong for
its own reasons and would have had the docs corrected to a false number. A control that cannot
fail has tested nothing.

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

- **The Price List API cannot price Fable 5 or Opus 5** — the models the seven harnesses run
  on, and the largest AgentCore line in the bill. Every `provider=Anthropic` entry for
  us-east-1 is Claude 3 or older. So realized billing rates (cost ÷ quantity from our own
  invoice) outrank the published price list; Price List is the fallback for never-used
  resources. It *does* price DeepSeek-R1, to within <0.001% of our realized rate — an earlier
  claim to the contrary was wrong because the `model` attribute value is bare `R1` (with
  `provider=DeepSeek`), which eyeballing the model list misses. Query by filter, not by eye.
- **Cost allocation tags are unusable today** — `project`/`Project` both Inactive, and a
  tag-filtered CE query returns **$0.00** for a day with real spend. Attribution is therefore by
  explicit resource match (`run_id` is already inside job and endpoint names), which needs no
  tagging at all. Tags are not retroactive, so the $10.77 run will never carry one.
- **Attribution must never be by service.** This account's month-to-date total was **$27,491**
  while this project's share was **~$10–15**; the rest includes unrelated SageMaker Canvas
  (~$296) and a JumpStart Whisper endpoint (~$36.36/day). A service filter would have reported
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
