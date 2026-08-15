# Test results — consolidated evidence

[繁體中文](TEST_RESULTS.zh-TW.md) · [Architecture](ARCHITECTURE.md) · [Case study](CASE_STUDY.md)

Every claim below traces to a verification file in `deploy/evidence/` — each one
the record of real invocations on a real AWS account (identifiers redacted per
SECURITY.md). "Always invoke before declaring success" is the repo rule; this
page is its ledger.

| Phase | Gate | Result | Evidence file |
|---|---|---|---|
| 0 — scaffold | preflight + config validation + unit tests + offline dry-runs | ✅ | CI + [PROJECT_STATE.md](../PROJECT_STATE.md) |
| 1 — spine proof | data-prep harness invoke-verified live | ✅ PASSED | [VERIFICATION_phase1.md](../deploy/evidence/VERIFICATION_phase1.md) |
| 2 pilot — data generation | autonomous distillation cycle + self-remediation | ✅ PASSED | [VERIFICATION_phase2_pilot.md](../deploy/evidence/VERIFICATION_phase2_pilot.md) |
| 2 main — dataset | curated.jsonl + stats in S3 | ✅ PASSED | [VERIFICATION_phase2_main.md](../deploy/evidence/VERIFICATION_phase2_main.md) |
| 3 — training | ModelTrained via launch-and-release | ✅ PASSED | [VERIFICATION_phase3.md](../deploy/evidence/VERIFICATION_phase3.md) |
| 4 — eval + deploy | gates decided; endpoint smoke + teardown | ✅ PASSED as a pipeline (model FAILED its gates — see below) | [VERIFICATION_phase4.md](../deploy/evidence/VERIFICATION_phase4.md) |
| 5 — autonomy | hands-off e2e: trigger → state machine → agents → honest terminal state | ✅ PASSED | [VERIFICATION_phase5.md](../deploy/evidence/VERIFICATION_phase5.md) |
| FinOps — cost governance | 7th runtime live; every rate carries its provenance; nothing publishes a guess | ⚠️ PARTIAL — runtime + arithmetic proven; rate card blocked on an IAM apply | [VERIFICATION_finops.md](../deploy/evidence/VERIFICATION_finops.md) |

## Static and offline checks (repeatable, CI-enforced)

| Check | Result | How to reproduce |
|---|---|---|
| Unit tests (contracts, cost model, driver loop, Lambdas, state machine document) | **1371/1371 passed** | `.venv/bin/python -m pytest tests/ -q --ignore=tests/golden` |
| Shell suite — N-way capacity race guard (`tests/test_capacity_race_guard.sh`) | **10/10 assertions** | `bash tests/test_capacity_race_guard.sh` |
| Negative controls — every guard broken in turn, confirmed to fail | **357/357 negative controls** | `.venv/bin/python tests/negative_controls/monitor_dispatch.py` |
| Harness config validation (5 specialists + conductor + auditor) | **7/7 `RESULT: OK`** | `python deploy/validate_config.py --config agents/<a>/harness.json` |
| Architecture SVG geometry (no wire crossings, no wire through a card) | **CLEAN** | `python tests/test_svg_geometry.py docs/architecture-*.svg` |
| Redaction scan (account IDs, credentials, account-bearing ARNs) | CLEAN | `.github/workflows/redaction-check.yml` |

### Live probe — the triage liveness loop (#37), against what Lambda is serving

Not CI-enforced and deliberately not a unit test: the non-run half of the resurrector is
the one path whose healthy state is indistinguishable from a broken one. A triage dies
maybe once a month, so `checked_liveness: 0` is the normal reading on almost every day —
and a Query against the wrong partition, a missing `dynamodb:Query` grant, or two
separately-bundled copies that disagree about the string `__liveness__` would each
produce exactly that same reading. So this probe downloads the **deployed** driver and
resurrector bundles and drives the whole loop against the **real** `llmops-stage-events`
table.

| Check | Result | How to reproduce |
|---|---|---|
| Non-run (triage) liveness beat → sweep → claim → revive → cap-escalate → terminal delete | **18/18 checks passed** against the deployed bundles (2026-08-12), 18/18 against this checkout | `python tools/probe_liveness_resurrection.py --region us-east-1` |

No agent turn (the fake AgentCore client raises before the first invoke, so the real
heartbeat runs and the billed part never starts), no real `lambda:invoke`, no real
`events:PutEvents`, and the resurrector's run-row half is handed an empty stub because
forcing `STALE_MINUTES=0` would otherwise claim and revive every live run. The one real
mutation is a single item in the dedicated `__liveness__` partition under a synthetic
subject, deleted before exit.

**Mutation-checked, 6/6 killed:** the resurrector reading a different partition (11/18) ·
the beat dropping `params` from the stamped payload, which is what a revival triages from
(15/18) · a terminal return marking instead of deleting (17/18) · the cap path escalating
against the triage itself rather than the run it was about (17/18) · the beat losing its
`attribute_exists(run_id)` condition and minting a ghost run row (4/6) · the sweep losing
its freshness guard and reviving live triages (16/18).

That fifth mutant is why the probe deletes a ghost row as well as reporting it: the row it
left behind **inverted the next probe run** — with a runs row present the conditional beat
*succeeds*, so the handoff into `__liveness__` never runs and every check below it fails
for entirely the wrong reason.

## Live invocations per phase

| Phase | What ran on real AWS | Key verified facts |
|---|---|---|
| 1 | data-prep harness created → memory → observability → invoke-verify | skills listed from a **git** mount — which is what every mount was on 2026-07-28; all 19 moved to `s3` in v1.2.0, so this line records what ran, not what runs; `aws sagemaker list-training-jobs` exit 0; S3 write confirmed orchestrator-side (`head_object`, 80 bytes, 1 s skew); memory active (2 sessions, 10 extracted memories, 0% error); logs + X-Ray delivering. 6 live defects found and fixed in the same loop (incl. `temperature`/`top_p` deprecation for Claude ≥ 4.7 — surfaced only at INVOKE time) |
| 2 pilot | 8 ARC-AGI-2 tasks distilled via DeepSeek-R1 (`us.deepseek.r1-v1:0`) | agent self-diagnosed token truncation from `stop_reason` (8k → 32k: format validity 1/8 → 8/8); `pilot_raw.jsonl` 213 KB verified in S3; 2 stream interruptions salvaged same-session |
| 2 main | 24-task generation + 5-stage curation | `main_stats.json` read back from S3: 8/24 solved, 74 attempts, best-of-4 early-stop (~40% token savings); curation re-verified every grid against ground truth, dropped 16 wrong-answer records; final 6 train / 2 val |
| 3 | QLoRA training (ml.g5.2xlarge) via launch-and-release | job Completed, 431 s billable; train_loss 0.5013 / eval_loss 0.5199; artifacts (adapter + merged bf16 + metrics.json) verified in the tarball; EventBridge → resume-Lambda chain observed twice (1.5 s, 0 errors); zero OOM at 14336 ctx with Liger fused CE |
| 4 | deploy → smoke → quality gates → teardown | endpoint v5 InService after 4 root-caused failures; smoke test answered a rotation task correctly over HTTPS; gates evaluated twice, FAILED honestly (below); teardown zero orphans (5 models + 5 endpoint-configs removed) |
| 5 | conductor + triggers + 5 hands-off e2e iterations | conductor produced a costed 5-stage plan from a natural-language goal ($29.09 estimate, 3-tier cost guardrails); webhook live (403/202); final e2e run traversed 7 states, zero human intervention, honest terminal state |

## The e2e gauntlet — 5 iterations, one real defect each (Phase 5)

| # | Reached | Defect found | Fix |
|---|---|---|---|
| 1 | DataPrepGenerate | Lambda roles lacked `events:PutEvents` on the custom bus | 3 roles extended |
| 2 | FinetuneLaunch | InvokeHarness takes `harnessArn`, NOT `harnessId` (unit-test fakes can't catch API contracts) | SSM name→ARN resolver in the driver |
| 3 | DataPrepGenerate | mid-swap harness version propagation window hid inline functions | stabilize configs before runs; single model per fleet |
| 4 | Deploy (7 states) | driver Lambda 900 s vs 840 s harness turns = one turn per invocation; `Sandbox.Timedout` killed a finished-but-unreported turn. Also: `gate_passed=null` promoted by a fail-open default | between-turn self-reinvoke (continuation payload); **gate fail-closed** (`is True` only) + regression test |
| 5 | RemediateFinetune → honest EscalateFail | — none — | — |

Run 5's terminal sequence is the platform working as designed: eval said
`FAIL_CLOSED_NO_INPUT` (a 2-sample mini-run has no quality signal), the machine
armed the remediation loop correctly, and the finetune agent answered
`REMEDIATE_PREMISE_INVALID — no quality signal to remediate` → `escalate_human`
rather than burning iterations. Zero orphaned endpoints; 4 `stage_complete`
events in DynamoDB; training cost $0.14.

## The quality gate that honestly failed (Phase 4)

16 held-out ARC-AGI-2 tasks (training tasks 25–40, never seen in training);
teacher baseline DeepSeek-R1 on the same tasks: 3/16 (18.75%).

| Iteration | Budget | Student solve | Format validity | Gate |
|---|---|---|---|---|
| 0 | 2,048 tokens (sync) | 0/16 | 18.75% | FAILED |
| 1 | 7,000 tokens (streaming) | 0/16 | 18.75% | **FAILED — final** |

Why the verdict is trustworthy (the eval agent's own controls): a lenient
re-scan of outputs still found 0 solves (not an extraction artifact); control
prompts through the identical client path returned coherent, well-formed grids
(pipeline and parser are sound); and the diagnostic that explains it —
**`closed_think_rate` 0%**: no output ever closed its `<think>` block, median
generation 5,831 tokens, 12/16 stopped by the context limit. The student
learned to *start* reasoning but never to *converge* — the documented
consequence of 6 training traces, far below the transfer floor for ARC
reasoning into a 1.7B student. The pipeline verdict PASSED; the model verdict
FAILED; neither was adjusted to flatter the other.

## Cost

| Item | Cost |
|---|---|
| Phase 2 (teacher tokens: pilot $0.69 + main $5.60) | **$6.29** |
| Phase 3 (431 s successful training ≈ $0.14 + ~$0.50 failed-startup minutes) | **≈ $0.64** |
| Phase 4 (~3.9 endpoint-hours across the 5-version arc + eval teacher tokens) | **≈ $4** |
| Phase 5 mini-runs | **≈ $1** |
| **Total, all phases** | **≈ $12–15** |

The entire test-proven record — six agents, a trained model, a deployed and
torn-down endpoint, five e2e iterations — cost less than this account spent in a
single day on one idle endpoint nobody was watching ($36.36/day, §4 of
[COST.md](COST.md)).

## FinOps — the 7th runtime, and what two failures verified (2026-07-31)

Full record: [VERIFICATION_finops.md](../deploy/evidence/VERIFICATION_finops.md).

| Check | Result | How to reproduce |
|---|---|---|
| Unit tests (all suites, incl. `test_cost_model.py` + `test_finops.py`) | **1371 passed** | `.venv/bin/python -m pytest tests/ -q` |
| Harness config validation, 7 agents | **`RESULT: OK`** | `python deploy/validate_config.py --config agents/finops/harness.json` |
| Live fleet | **7 harnesses READY** | `list_harnesses` via the repo's vendored boto3 |
| Canonical module has a distribution path | prints `would upload 4 contract files` | `python deploy/03_storage.py --region us-east-1 --account-id 123456789012 --dry-run` |

Every guard added in this work was **mutation-checked**: the asserted behaviour was
reverted one at a time and the test confirmed to fail — **357/357 negative controls**, 308
mutations asserting 357 (guard, mutation) pairs, one printed PASS line each. A test that
passes both with and against the behaviour it names is not a test.

The count is in the sentence on purpose. "Mutation-checked" is an adjective, and an
adjective cannot go stale: a control deleted, or a guard added with no control at all,
left that sentence still reading true.
`tests/test_docs_claims.py::test_the_documented_negative_control_count_matches_the_runner`
now derives both numbers from the runner's own `case(...)` registrations.

A derived count is still only a count, and one of these controls was not passing. A full run
found **m189 UNCAUGHT** — the control that strips the ranking out of the eval prompt's
val-split sentence, leaving the customer's acceptance set and the 10% val split reading as
equally eligible. Its guard searched the whole bullet for `/fall back/`, and the same bullet
says *"never fall back to the newest artifact you can find in the bucket"* about `eval_only`'s
`model_artifact_uri` — an unrelated sentence that satisfied the regex whatever the prompt said
about the two evaluation sets. So the previous count was reported as all-passing while a
mutant survived: what the number proves is that a control EXISTS per guard, not that it kills.
The guard now scopes the search to the sentences that name the val split, and m189 fails as it
should. Anything that can be satisfied by a sentence written for another reason is not a
guard, and only a full run says which ones those are.

There is a third way for a control to report PASS without verifying anything, and the next full
run found it one case after it was written: **m236 killed its guard with an `IndentationError`.**
It deleted two lines from inside a `try:` block and left the body empty, so every test that
imports the console errored during collection — and pytest exits `1` for a collection error and
for a failed assertion alike, so the runner's own check (`rc == PYTEST_TESTS_FAILED`) cannot tell
them apart. The only trace was the kill mode in the printed tail (`1 error in 0.28s` where every
other case says `1 failed`), which nothing reads. Both the runner and
`test_every_negative_control_still_matches_the_code_it_mutates` now `compile()` every mutation of
a `.py` file and refuse one that does not parse — Python sources only, because breaking a JSON or
Markdown parser is frequently the exact break a control asserts. A mutation has to reach the code
to prove the guard watches it.

The counts above also went up for a reason worth stating, because it is the opposite of the usual
one: **two of the newest controls exist because a guard was pinning a bug.** The eval prompt
decides a scalar gate that reports no interval by a fixed ±0.05 band — borderline at or within
that distance of the bar, escalate rather than decide. The console had no band on that branch at
all, so it painted PASS across the whole of it, *including at the bar itself*, where the distance
is 0 and the rule is maximally borderline; two existing assertions said that was correct. And the
band is the wrong instrument for the gate the live plans actually carry: `format_validity ≥ 0.95`
on a rate whose ceiling is 1.0 puts the **entire** passing region inside the borderline band, so
no value that clears the bar can ever pass decisively — 96 of 97 valid answers is an escalation
the page called a pass. The fix is the interval that proportion is entitled to (97/97 → Wilson
lower bound 0.9619, a decisive pass; 96/97 → [0.9439, 0.9982], an honest borderline), so the eval
prompt now mandates `<metric>_ci_low/_ci_high` and `<family>_n` for every proportion it reports,
and the band survives only as the fallback for a metric with no denominator — with a clause
requiring the agent to say so, once, when `bar + band` reaches the metric's ceiling. A guard whose
assertion encodes the shipped behaviour is not evidence that the behaviour is right.

The newest twelve controls are about a state rather than a number. A blocked run **could be
answered, or noticed — never both**: `checkpoint` keeps a run answerable and notifies nobody,
`escalate_human` notifies and makes the run unanswerable, and the eval gate's *borderline*
verdict — the one gate outcome a human answer can settle — was routed to the second. Half the
fix is a third channel (`page_human`: notify, keep the run alive), and half is that a stage's
page must **not** end the turn, because the invocation is holding a task token `_ack_terminal`
does not settle: `EvalGate` would wait its full `TimeoutSeconds: 86400` on a token nothing is
left alive to settle, so an agent doing exactly what the prompt asks would hang its own run for
a day. Both halves are mutation-checked, and so is every bound around them — m245 refills the
waiting-turn counter at each Lambda boundary (a real wait crosses several, so the count would
read 1 forever), m249 counts surviving rows instead of reading each row's own `waiting_turn`
(past the driver's 12-row cap a count is not even a floor, and it under-reports the longest
waits), m250 reads the oldest rows instead of the newest (the pill would vanish on exactly the
long runs that wait longest), and m251 swaps the console's prefix match for exact status
equality (a richer terminal status would draw a pill whose answer the driver refuses to park).
A state the operator cannot see is a state the system does not have — the third time this
repo has paid for that sentence.

**D13 — a window taken before the ordering is a window on hash order (twelve controls,
m255-m266).** Every list in the console read `scan(Limit=N)` and *then* sorted by time, so
`Limit` did not trim the oldest rows: a Scan's item order is documented as unspecified, and
what it trimmed was whatever fell past the page boundary. Measured on the live `llmops-tasks`
table (35 rows, `Limit=25`): **6 of the 25 newest consultations were absent from the list**,
among them one in status `error` and one `drafting` consultation waiting on a human signature,
while 6 older ones were shown in their place. The same shape sat in four more places, three of
them still latent: the run list (arbitrary window past 60 runs — so the run an operator just
started can be missing, and a run that cannot be found is a run that gets started again),
`list_optimizations` (the window came before the `opt-` filter, so drafts read as never having
existed), the estimates GSI fallback (hash order under a docstring promising newest-first), and
`_timeline`, whose events half spent its budget FORWARDS while its directives half already read
in reverse — one function, two directions. That last one is the interaction worth naming: it was
harmless while runs held ~16 events, and D12 raised the ceiling to ~150 (`WAIT_ROW_CAP` is 12
rows per stage invocation), so **the previous fix pushed the real event count past this one's
window**. The controls pin the parts that make the difference between a number and a wrong
number: m261 infers truncation from `len == limit` instead of reading one extra row (a run with
exactly 100 events would claim history nobody read), m262 serves the reverse window without
restoring time order (so the frontend's `slice(-25)` paints the oldest 25 of the newest 100 — a
window that is right at neither end), and m256/m257/m263/m264/m265 each drop one truncation
marker on its way from the query to the screen. A capped list that does not say it is capped
reads as complete, which is the same defect one level up.

**D14 — a fix that reached five of seven harnesses, 43 memory records its obvious repair would
have burned, 63 an earlier deploy already had, and 9 no spelling this repo owns could name
(twenty-two controls, m267-m288).** The shared BYO memory is wired by
`deploy/04_wire_memory.py`, whose harness list was hand-written: the five pipeline workers.
Two harnesses are wired to the same memory and were not on it, so #83's retrieval tightening —
semantic `topK` 10 → 5, `relevanceScore` 0.2 → 0.6, the fix that stopped another run's
post-mortem being injected as a bare fact — never reached them. Measured live on 2026-08-13:
`llmops_finops` and `llmops_orchestrator` still sat at **10 / 0.2**, the exact pre-fix setting,
while all five listed harnesses carried 5 / 0.6. Nothing failed; the channel just stayed as
loose as it had always been, on the two agents whose prompts are *built* on memory (finops:
"estimate accuracy improves only if each reconciliation's finding survives into the next
estimate"; orchestrator: "your memory is shared with the specialists"). And `deploy/05_harnesses.py`
already carries a comment naming this exact failure mode — *"a config on disk that no script
names is a harness that silently never exists"* — so the lesson was written down in one script
and not applied in its sibling.

The obvious repair is destructive, which is why it was measured before it was written.
`actorId` is the **partition key** of every namespace (`/users/{actorId}/facts`), and those two
harnesses were wired by the older `deploy/wire_memory.py`, whose `--actor-id` took the full
harness ID. Live: `/users/llmops_finops-eDJtU9PvKh/facts` holds **13** records,
`/users/llmops_orchestrator-GsIqHZ4viJ/facts` holds **30**, and every bare-name partition holds
**0**. Deriving the list and letting the script's preferred spelling win would have abandoned
all 43 in one call — and `UpdateHarness` returns success either way, so the deploy that finally
applied the retrieval fix would have been the deploy that threw away the memory it exists to
serve. So a live `actorId` now wins over the one this script would choose, moving one is
opt-in per harness (`--repartition <harness>`), and a move refuses to proceed unless the data
plane can say what it costs — an unknown record count reads exactly like a count of zero
(m272). The controls hold both halves apart: m270 lets a redeploy rewrite a live `actorId`,
m271 makes `--repartition` fleet-wide, m273 stops the count at its first page (under-reporting
exactly the partitions large enough to matter), m275 loosens the semantic channel back to the
episodic setting — which is only safe for episodic recall because `{sessionId}` scopes it to
the agent's own session and nothing scopes facts. The guard that should have caught all of this
asserted `wired == {data-prep, finetune, eval, deploy, monitor}`: **it agreed with the omission**,
the same shape as the console gate band. Derived from the configs instead, it failed immediately
and named a third gap nobody had looked for — the orchestrator prompt had no memory-precedence
rule either, and neither did finops (m276, m277), the two agents that publish a rate card and
quote a price to a human.

**And then the measurement behind that fix turned out to be too narrow, in the direction that
mattered.** Only two full-harness-ID partitions had been counted — 13 + 30 — because only those
two harnesses still *pointed* at one. Counting all seven:

| partition | records | live `actorId` |
|---|---|---|
| `/users/llmops_data_prep-KuSKXUaxyP/facts` | 2 | the bare name |
| `/users/llmops_finetune-xXl7jsACZO/facts` | **25** | the bare name |
| `/users/llmops_eval-iuIIs96fFM/facts` | **16** | the bare name |
| `/users/llmops_deploy-nLLNWairTc/facts` | **11** | the bare name |
| `/users/llmops_monitor-YCXC5hcXzu/facts` | **9** | the bare name |
| `/users/llmops_finops-eDJtU9PvKh/facts` | 13 | that partition |
| `/users/llmops_orchestrator-GsIqHZ4viJ/facts` | 30 | that partition |
| `/users/<every bare harness name>/facts` | **0** | — |

**106 semantic records, of which 63 are already unreachable by the agent that wrote them** — and
the episodic channel is stranded the same way, 105 more records under the five workers'
`/episodes/<full id>`. `llmops_monitor`'s newest orphaned record is dated **2026-08-08**, so the
move is days old, made by an earlier run of this very script, by exactly the mechanism above.
The two harnesses that were spared were spared *only* because the hand-written list omitted
them: the defect was the reason the data survived. The guard that keeps a live `actorId` is
therefore real but late — it protects the 43, and no API moves a record between namespaces, so
the 63 are a report rather than a repair.

What was actually missing is smaller and duller than the wiring bug: **nobody ever counted the
other spelling**, so "this agent has no memory" and "this agent's memory is 25 records away from
here" printed identically in a deploy log. Every attach now reads the partitions held under any
spelling of `actorId` it is *not* being wired with, and the controls hold each part of that
sentence: m278 silences the report, m279 drops the episodic namespace (reporting the smaller
half of the loss), m281 checks only the full harness ID (fine for six of seven live harnesses,
blind in the case worth catching), m282 runs the check only when a repartition is requested —
which is never for the five that had already lost theirs — m283 counts one page, and m280 turns
an *unread* partition back into an empty-looking one, the equivalence that let 63 records leave
without a single failed call.

**And that check was still one assumption short.** It compares the two spellings *this repo can
produce* — the bare harness name and the full harness ID. `ListActors` returns **16** actors on
this memory, and two of them are neither: `monitor` holds **3** semantic records and
`monitor-agent` holds **6**, `actorId` values that appear in no file in this repo (the older
`deploy/wire_memory.py` takes `--actor-id` as free-form text). So 9 records were invisible to a
guard whose own docstring claimed to cover "an `actorId` that is neither" — it covered the case
where the *live* id is a third spelling, not the case where a third *partition* exists. A
candidate list written from a repo's naming conventions cannot contain a spelling the repo never
had, so the sweep is now derived from the data plane's own enumeration instead, once per run:

| check | derived from | live finding |
|---|---|---|
| per-harness `stranded_partitions` | this repo's two spellings | 63 semantic + 105 episodic, attributed to a harness |
| memory-level `unreachable_actors` | `ListActors` | **9 orphaned actors, 72 semantic + 108 episodic = 180 records** |

The two overlap and are **not** additive — the first says *which harness* lost a partition, the
second says whether anything on the memory is orphaned at all. Five controls hold it: m284
builds the reachable set from this run's own attach list, so `--harness llmops_eval` announces
the other six harnesses' healthy partitions as lost (a warning that cries wolf six times out of
seven is a warning nobody reads); m285 reports zero orphans for a memory that does not exist yet;
m286 counts only the semantic channel, so the actor `finops` — 0 facts, 1 episodic record —
reads as clean; m287 reads only the first page of actors, correct exactly while the memory stays
small; and m288 removes the call from `main()` altogether, the case where a guard exists in the
repo and not on the deploy path.

### Two failures worth more than the passes

**Denied billing reads.** The auditor reported *"Priced SKUs: 0. Unpriced: all"*, stated
the existing card's freshness as **unknown** rather than assuming, **declined** to call
`update_rate_card`, and named the exact missing permissions.

**Denied S3 and its own arithmetic module.** It derived a complete **37-SKU rate card with
`unpriced: []`**, then refused to publish it — stamping its own output
`v1-DRAFT-noncanonical` because the `fallback_static` tier lives inside the module it
could not reach, and saying so in its first line.

The failure mode that would have mattered is a confident-looking card nobody can
regenerate next month. **Someone approves a five-figure run on these figures.** Fail-closed held
under conditions nobody designed for.

### Rate provenance, measured

CE-realized and Price List agree to **<0.001%** on the 5 SKUs both carry — realized rates
are trustworthy as primary. But every Anthropic entry in Price List for us-east-1 is
`Claude 2.0 · Claude 2.1 · Claude 3 Haiku · Claude 3 Sonnet · Claude Instant`: **no
Fable 5, no Opus 5 — the harness fleet's own LLM usage, the largest AgentCore line.** A
Price-List-only refresh silently zero-prices it.

A planning finding was **corrected** here: Price List *can* price DeepSeek-R1. The `model`
attribute value is bare **`R1`** (`provider=DeepSeek`), so scanning 84 values for a name
containing "DeepSeek-R1" finds nothing and wrongly concludes absence.

### Four defects that only deploying could find

Each was repo-complete, documented, unit-tested — and would never have been created:
a harness config absent from `AGENTS`; a scheduled function with no `LAMBDAS` entry
(a daily `ResourceNotFound` visible only in the scheduler's own metrics);
`update_function_configuration` never passed `Role`, so a role change reached only
functions that don't exist yet (**measured**: `"updated"` reported while the live function
kept its birth role); and an execution role with no `finops/*` grant.

**Not yet published:** the rate card and estimator validation against the measured
**$10.77** ground truth are blocked on an IAM apply. Everything above is `provisional` by
CE's own `Estimated: true` flag — publishing it as settled is the one thing the prompt
forbids.
