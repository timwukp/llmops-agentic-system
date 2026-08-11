# Re-connect brief — llmops-agentic-system

Written 2026-08-10 while you were at dinner, so a lost session costs nothing. Standing
instruction in force: **先繼續 resolve llmops pipeline 所有的 bug 再說**, and
**所有的 bug，必須根治** — no patches.

## State right now

`1074 passed, 0 failed` (27.4s). Negative controls **226/226** (193 mutations, 226 pairs,
226 printed PASS lines, 0 FAIL, 0 SKIP-BROKEN, runner exit 0). Redaction scan clean, 163
files; the real account id appears in **zero** files repo-wide. **Nothing committed, nothing
pushed.** 39 tracked files modified in the working tree — the last several bug fixes are all
uncommitted together, which is the thing most worth knowing if this session dies.

## Just finished — bug #22 (task #29), no stage could read the stage before it

**The autonomy blocker, and the reason it is worth naming that way**: the user's goal is a
platform that runs itself, reflects on problems, and iterates. Every specialist prompt calls
the S3 manifest "the single source of truth" and is told to read it first and append its
results. The driver assembled each finished stage's `{status, outputs, metrics, evidence}`
into a **local variable**, handed it to `write_run_report`, and dropped it — it had **no
`put_object` for the manifest at all**.

Measured by driving the real driver: after a deploy stage reported
`metrics.endpoint_name=llmops-student-run-1`, `manifest.stages` was still `{}`. The run
**report** carried every metric. So the write reached the doc **humans** read, not the one
**agents** read. Fifth instance of "two correct halves, never connected" — but the first
where the lost information is not a signature, it is **a fact the run itself produced**.

1. `params.student_endpoint` is read by eval and monitor and was written by **nothing**. An
   endpoint name does not exist until deploy creates one, so **no plan can be signed with it
   and no default can stand in for it**. `models.student.endpoint_name` is in
   `manifest.schema.json` and nothing ever wrote that either.
2. finetune's *analyze* task is told to diagnose from artifacts the manifest does not list.
3. **An agent asked to reflect on a run had only its own turn to reflect on.** A pipeline
   whose stages cannot read each other's results cannot iterate on a run; it can only redo it.

Fixed in both halves. **Producer** `_save_manifest`: read-modify-write (the driver is the
*second* writer — `S3PipelineObjects` grants the harness role `PutObject` on `runs/*` and 5
of 7 prompts tell the agent to append here, so a blind put erases the agent's own turn),
narrowed to `stages` alone (a driver that can rewrite `models`/`plan`/`approval`/`params` is
bugs #9/#20/#21's exact defect — `IMMUTABLE_MANIFEST_KEYS`, taken from the S3 copy, never the
driver's), absent manifest **refused and reported**, never manufactured. **Consumer**
`STAGE_FACT_PARAMS`: carries a prior stage's reported facts forward under the names the
prompts already read, mirroring `MODEL_PARAM_FOR_ROLE`; absent facts **omitted, never
guessed**, because a metric attributed to the wrong endpoint reads as evidence.

S3 has no compare-and-swap, so the write is *narrowed*, not atomic. The ASL is fully serial
(no `Parallel`/`Map`), so two stages never complete at once; the residual window is one
stage's agent writing between the driver's read and its put.

**6 new guards, 6 controls (m180–m185), 9 pairs.** m164/m165 needed re-anchoring (their
anchor was the params merge line I edited) — the anchor-drift guard caught that, as designed.

### Three of my own defects in this fix, all found by distrusting a green run

- **`1060 passed` was not evidence.** `_save_manifest` sits inside the report `try` whose
  `except` degrades to `report_error`, so a `raise` is swallowed and every test still passes.
  Read the control flow; do not read the exit code.
- **`IMMUTABLE_MANIFEST_KEYS` was referenced by nothing** — 12 lines of comment declaring an
  invariant no code executed. Prose is not verified. Now a real tamper check.
- **The `raise` was nearly unreachable**, because I had left an `if manifest:` around the
  write: an absent manifest was skipped **silently**, which is bug #22's own failure mode
  surviving inside the fix for it. Dedented (my first attempt used `if True:` — the exact
  shape m178b exists to catch).

### The derived guard is the real deliverable, and its first version was near-vacuous

`test_every_param_a_prompt_reads_has_something_that_writes_it` enumerates all 25 `params.X`
the 7 prompts read and pins each to the mechanism that supplies it — `DEFAULT_PARAMS`, a
signed plan, the driver (`MODEL_PARAM_FOR_ROLE` / `STAGE_FACT_PARAMS`), or the dispatch
event. Both directions fail: an unclassified param, **and** a classified param no prompt
reads (that one catches dead wiring). Bugs #20/#21/#22 were one shape found by hand three
times; this is how instance #6 fails in the suite instead of in a run nobody can explain.

**Its first version was load-bearing on exactly ONE of the 25 params.** I let "a plan is
*permitted* to carry it" count as a writer, and `student_endpoint` satisfies that vacuously —
the guard would have stayed green with the driver's injection deleted. Being permitted to
carry a field is not the same as anything writing it. I found this by asking, of the
assertion, *which params can it actually fail on* — a question worth asking of every derived
guard.

Two controls were also wrong rather than the guards they named, same lesson as bug #21's two
escapes: m180 named a test that drives `_run_stage` against a manifest already holding
`stages` (it reads the forwarding, not the write-back), and m183 mutates the **wiring** while
the derived guard reads the **declaration** — so the declaration got its own control, m185.

### Two suspects withdrawn, and one that was never a bug

From bug #21's suspect list: **`sweep_uri` IS written** (`monitor_sweep/handler.py:96`) and
**`variance_threshold_pct` IS written** (`finops_reconcile/handler.py`). And
**`params.budget_usd` is not a bug of this class** — the consult prompt calls it advisory and
explicitly optional ("compare it to params.budget_usd if given"), no structured field
captures it, the customer states it in prose in the goal, and consult is dispatched by the
**console**, so `PLAN_META_KEYS` never touches it. Absence is the designed behaviour. I had
this wrong before compaction and am recording the correction rather than carrying it forward.

## Before that — bug #21 (task #28), the REST of the signed plan

Bug #9 cured model consent being overridden; bug #20 cured the *name* it is written under.
**Both left every other field of the plan behind.** `seed_manifest` consulted `plan` for
models and merged everything else as `{**DEFAULT_PARAMS, **params}`, so ARC-shaped defaults
silently replaced what a human signed. Measured on a signed industrial-defect plan:

1. Priced on `ml.p4d.24xlarge` / 40 000 samples / `{"map50": 0.75}`; **ran** on
   `ml.g5.2xlarge` / 2 000 samples / ARC's `relative_solve_rate`. 8 fields replaced,
   `domain` dropped entirely.
2. `plan.data.{source_uri, customer_eval_uri}` never reached the **flat** params
   data-prep's audit prompt reads — an audit arrived with **no data URI**, and its prompt
   correctly forbids guessing. Fourth instance of "two correct halves, never connected".
3. **`pipeline_mode: data_audit` dropped** → the ASL's `StartAt` Choice reads that key from
   the *execution input* with `Default: DataPrepGenerate`, so an audit customer got GPUs.
4. The console — the only path a customer has to sign a plan — forwarded **no `plan` at
   all**; approve→launch scraped it for two integers.
5. Consumer half: eval's gate task named its own bar (`0.80 x teacher`) instead of reading
   `params.gates`. **A gate is the one place "the agent used its judgment" is unacceptable,
   because the gate is what the signature is FOR.**

Unobservable afterwards, which is what makes the class expensive: the variance report joins
the estimate to the actuals and reads the gap as an **underspend**, not as two different runs.

Fixed: `PLAN_META_KEYS` (a **denylist** — an allowlist omits the field nobody thought of, and
a default hides the omission, which is exactly how `pipeline_mode` and `gates` went missing) +
`_plan_params` (flattens `data` via `setdefault`, so an explicit top-level key still wins) +
`_merge_params` (`DEFAULT_PARAMS` < `params` < plan, disagreement refused by name, compared
against the **flattened** plan). Console forwards the priced plan; eval/finetune/deploy
prompts de-hardcoded. **This subsumes the genericity item** "DEFAULT_PARAMS hardcodes
arc-agi-2" — those defaults were only harmful because a plan could not displace them.

9 new guards. **10 controls (m171–m179 + m178b), 10 caught, 13 pairs.**

### Two controls escaped, and both were the CONTROL's fault, not the guard's

- `{**DEFAULT_PARAMS, **plan_params, **params}` — params outranking the plan — is
  **unobservable**: the conflict gate above it has already proven every shared key equal, so
  both orders are the same dict by construction. A mutation that cannot change an output is
  evidence about the control, not the guard. Replaced with the precedence that *can* be
  subverted: the gate's own **reach**, `set(plan_params)` → `set(plan)`, which leaves the
  nested half of every signed plan silently overridable — on `source_uri`, the one field a
  data audit is entirely about. The guard now asserts that case.
- The console guard reads `start_run`'s **source text** for `payload["plan"] =`, so it passes
  against an `if False:` wrapped around the block. Source-text guards cannot see reachability.
  Cured by `test_the_launch_payload_carries_the_priced_plan` in `test_console_cost.py`, which
  asserts on the payload the fake Lambda client was **handed** and derives its key set from
  the console's own `INT/FLOAT/STR/BOOL_KEYS`. m178 now deletes the assignment; m178b neuters
  the branch — two shapes, two controls.

## Before that — bug #20 (task #27), one model with four names

A plan is **priced** by `cost_model.py`, **resolved** by `start_pipeline`, **executed** by
the driver, and all three named the model differently:

1. The console form (the only path a customer has to sign a plan) posts
   `plan.teacher_model`; the resolver matched only `plan.models.teacher`. So `models` was
   absent → read as "the plan is silent" → fell to `DEFAULT_MODELS`. **Measured: a
   console-signed Fable-5 plan produced `manifest.models.teacher = us.deepseek.r1-v1:0`
   — priced as one model, executed on another, every artifact agreeing.** Bug #9's class,
   reintroduced through a *name* instead of a precedence rule.
2. A plan mirroring and licence-checking `meta-llama/Llama-3.2-1B` and assigning it to no
   role produced `manifest.student = Qwen/Qwen3-1.7B` — trains on an uncleared model.
3. Every prompt reads `params.teacher_model_id`; **nothing ever wrote it**, so agents fell
   back to the model hardcoded in their own persona line. Third instance of this repo's
   recurring shape: two correct halves, never connected.

Fixed: `ROLE_ALIASES` (accepted on read, since 3 of the 4 names are in unrewritable signed
S3 artifacts), self-contradiction refused, unknown `models` keys refused (`teachr` used to
mean silence), mirror-with-no-role refused; driver injects `manifest.models` into every
stage payload; 6 persona lines de-hardcoded, and `finetune` now picks its method from the
model — **full fine-tuning for a YOLO detector, where adapters do not apply**.

12 new guards. **25 controls registered (m146–m170), 25 caught, 31 pairs.**

### The methodology failure worth remembering

**My hand-run mutation evidence was wrong, and the runner's own docstring had warned about
it since #58.** CPython validates a `.pyc` against *(source mtime in whole seconds, source
size)*, and `{**approved, **payload["params"]}` / `{**payload["params"], **approved}` are
the same byte count — so a mutate-run-restore cycle inside one second ran the **mutated
bytecode against the restored source** and printed a catch. Bug #18's and #19's controls
had also only ever been hand-run; both are now registered too (m146–m153), which is why the
documented count jumped 165 → 196. **Register controls in
`tests/negative_controls/monitor_dispatch.py`; never mutation-check by hand.**

Three controls exist only because they escaped first: a guard intersecting cost_model's
field names with the dispatcher's alias list was blind in the direction the bug travels
(m161/m163); the mirror check was never tested against a near-miss, so
`hf_repo: …Llama-3.2-1B` + `student: …Llama-3.1-70B` passed a publisher substring (m159);
and the merge-order test recomputed the merge in its own body (m165).

## Before that — bug #18 (task #25), `deploy/02_network.py`

Audited to me as *"~$2.64/day is being spent on an unused VPC."* **The premise was false**
and measuring it first is what found the real defects: `describe-vpcs` and
`describe-vpc-endpoints` on `tag:project=llmops-agentic-system` in us-east-1 both return
`[]`. Nothing is deployed; $0/day is billed. Three real defects instead:

1. **The cost note was exactly half.** AWS bills an interface endpoint *"for each hour that
   your VPC endpoint remains provisioned in each Availability Zone"* — `SubnetIds` creates
   one ENI per subnet and the ENI is the billed unit (Pricing API `USE1-VpcEndpoint-Hours`
   = $0.01/hr, checked, not remembered). 11 endpoints × 2 subnets = **$5.28/day**; the
   printed `0.01 × 11 × 24` was the one-AZ answer. Now derived from
   `len(INTERFACE_SERVICES) × len(subnet_ids)`.
2. **All 11 were provisioned for nobody**, then a warm success line. No
   `harness.prod.json` (never existed), all 7 configs `networkMode: PUBLIC`,
   `07_lambdas.py` has `VpcConfig` zero times, `/llmops/network/*` read by nothing. Now
   skipped by default via `find_endpoint_consumers`, which reads the files a deploy reads
   so it inverts on its own; `--force-unused-endpoints` overrides.
3. **`ARCHITECTURE.md` §11 claimed the Lambdas "can run VPC-isolated"** — a capability
   with no deploy path, and load-bearing on spend because it implied the missing consumer.

Gate is on the billing line, not the script: the free substrate (VPC, subnets, SGs,
gateway endpoints, SSM) is still built, because that is what a `harness.prod.json` must be
written *against*. Exit stays 0 — nothing failed, nothing half-applied; the signal is
stderr + `interface_endpoints: false` in the JSON.

7 new guards. **9 mutations applied, 9 caught.** Worth knowing: `want_interface = True`
passed every guard I had until I added `test_main_withholds_...`, which drives the real
`main()`. Two correct components wired together wrongly was the original bug's shape, so
the wiring needed its own test.

## Next, in order

1. **Audit item 1.2 — DONE, and it was not a decision after all.** I had written here that
   it needed your call between building a `HumanGate` ASL state and conceding the platform
   has no human-in-the-loop pause. **Measuring dissolved the fork**: the platform already
   has a working live pause — `checkpoint` — and the bug was that five `escalate_human`
   descriptions plus six TURN-END INVARIANT bullets pointed blocked agents at the terminal
   exit instead. A `HumanGate` state would have been a *second* pause beside a working one.
   Fixed as wording, not architecture; 3 new guards, 11 mutations applied, 11 caught, plus a
   verified stand-down if escalation ever becomes recoverable. See CHANGELOG `[Unreleased]`.
2. **The genericity item is CLOSED by bug #21, and it was never the defaults.** I had
   written here that `DEFAULT_PARAMS` at `start_pipeline/handler.py:31` (`dataset:
   arc-agi-2`, `keep_reasoning`, the `relative_solve_rate` + `format_validity` gates) was
   the one hardcoded coupling to remove. Measuring inverted it: those values are *correct*
   as the fallback for a run nobody planned, and the actual defect was that a signed plan
   **could not displace them**. Deleting them would have left the same bug with an empty
   dict. Now a plan naming a COCO dataset and a `map50` gate wins. Audit part 2 (2.1–2.6)
   is still open and genuinely additive: logit/on-policy distillation, wiring
   `pipeline/v2`, `data-prep/blend`, model-registry contract, `teacher_licence` gate,
   tiered evaluation.
3. **The bug-#21 suspect list is CLOSED — every entry is now driven end to end.**
   `student_endpoint` was the one real bug (#22, fixed). `sweep_uri` and
   `variance_threshold_pct` are written and were my error. `budget_usd` is advisory by
   design. `keep_endpoint`, `latency_p50_target_ms`, `sample_size` and `hf_token_secret` are
   plan-supplied and reachable since bug #21 (`PLAN_META_KEYS` does not exclude them), and
   `test_every_param_a_prompt_reads_has_something_that_writes_it` now pins all 25 so this
   list can never again be a set of unverified suspects. Still open:
   `pipeline/v2/augment.py:53` hardcodes `/Users/tmwu/Downloads/kaggle-arc-agi-2/...` in a
   module nothing invokes.
4. **Autonomy, in the user's own terms, now that #22 unblocks it.** The stated goal is a
   platform that runs itself, reflects on problems, self-learns and self-iterates. Persisting
   stage results was the precondition — before it, an agent had nothing to reflect ON. The
   next honest question is what the platform does with a *failed* run: `finetune`'s "analyze"
   task, the remediation `iteration` loop, and whether any stage reads its own prior attempt.
   Worth measuring before designing anything, on the #18 precedent where the audited premise
   turned out to be false.

## Blocked on you — I will not start these unasked

- **#16 deploy the driver** — needs a message naming the production target. "Merged" does
  not authorize a deploy.
- **#10 ARC-2 continuation #6, 55 tasks** — needs a fresh signature. **The estimate must be
  recomputed**: $48–60 is the stale 43-task number.
- Fable-5 A/B closeout (~$2.40).
- Any PR merge — yours, not mine.

## Cost, reported the way you asked — separately, never summed

**$5.99 verified** + **$101.98 `RECONSTRUCTED ESTIMATE — UNVERIFIED`**. Not $106.24.
Cap $400, budget $450.

## Process constraints that bit during this work

- `git push` **and** `git commit` are both hook-blocked. Only sanctioned path: GitHub Git
  Data API (`~/Desktop/github-git-data-api-push.md`; `tools/push_via_api.py` when local
  commits exist, `~/Desktop/push-uncommitted-via-api.sh` when commit is blocked too).
- **Never hardcode `mode:"100644"`** — read it from `git ls-files -s`, verify against
  `git/trees/<sha>?recursive=1`, not `contents?ref=`.
- Redact account IDs before any push. Verify every uploaded blob SHA against
  `git hash-object` — `gh api -f content=@-` can upload an empty blob with HTTP 201.
- Bilingual twins in the same PR. Test-count guard trips on every added test (1040 → 1060
  across this session); update both `TEST_RESULTS` twins, 2 sites each — **and** the
  control-count line, which is derived from the runner's `case(...)` registrations
  (165/165 → 209/209, and note the number is **pairs**, not cases: 180 mutations assert 209
  (guard, mutation) pairs because one control may name several guards).
- **Never mutation-check by hand.** `PYTHONDONTWRITEBYTECODE=1 -B` is not optional; the
  registered runner clears `__pycache__` per case and journals before mutating. See the
  methodology note above for what a same-second, same-size restore actually proves.
- The runner has **no `main()`** — it is a module-level `for` loop over
  `(CASES if __name__ == "__main__" else ())`, deliberately import-safe. To run a slice,
  exec the source with `__name__ = "__main__"` after splicing `CASES[:]`.
  **`tools/run_control_slice.py <lo> <hi>`** does this — it asserts the loop header still
  matches and refuses an empty slice, because a slice runner that selects nothing and prints
  "all guards caught their break" is worse than no runner.
- **A mutation that cannot change an output proves nothing.** Two of bug #21's controls
  escaped, and neither was a coverage gap: one was unobservable by construction, the other
  mutated reachability while its guard read source text. Before writing a control, ask which
  observable value the mutation moves — and if the guard is a `re.search` over a file, assume
  it cannot see `if False:`. Bug #22 repeated it twice more (m180, m183), both times because
  the control named a guard that reads a *different half* than the mutation moves.
- **Ask of every derived guard: which inputs can it actually fail on?** Bug #22's flagship
  guard enumerated 25 params and was load-bearing on 1, because an `or` clause let a
  permissive condition satisfy the assertion. Green plus wide coverage still hid it; the
  question found it in one script.
- **`timeout` does not exist on macOS**, and `cmd > log; echo "EXIT: $?"` after a redirect
  reports the *echo*'s status on some shells — both bit during this session and both make a
  broken run look clean. Capture the exit code of the run itself, on its own line, and check
  the printed PASS-line count against the documented number (226 here).
- **`grep -cE` with hand-escaped `\[`/`\*` silently mis-anchors.** An anchor check printed `0`
  for a line that was present, and ugrep errored on `{**`. Use `grep -cF` with a heredoc for
  anchor sweeps.
