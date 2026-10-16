# VERIFICATION — Phase 1 live evidence: a held-out acceptance set, and a gate that measures the model (PR-11, 2026-10-15)

## Why this PR changed the generator first

PR-09's live lakehouse run scored 32/32 on `gold.qlora_eval` and ended **borderline**: at n=32 a Wilson
95% lower bound cannot clear 0.95 even with zero failures. The owner chose to rerun the gate on n≥100
items. Measured first, offline, through the real transforms:

| What | Closest-match similarity to the training questions |
|---|---|
| v1: seed 7's own eval split vs its training split | median **0.935** (6 of 32 with an identical instruction) |
| v1: seed 8 / seed 11 rows vs seed 7's training rows | median **0.949 / 0.942** (22–23% identical instructions) |
| **v2: the acceptance set vs training** | median **0.538**, p90 0.645, max 0.762, **0 identical** |

v1 built every conversation from one or two templates per intent, so any v1 eval set, larger or not,
measured template recall. Shown this, the owner chose held-out question families plus a retrained
model, over re-judging the old model.

**Generator v2** (`generator_version: "2"`, `_FAMILIES`):
- Each (intent, language) has one policy answer, 4 train phrasings and 2 held-out phrasings.
- Conversations use only the train phrasings.
- The acceptance set (`eval_prompts.jsonl`) has 120 items at demo scale. They are phrased only in
  held-out wording, each with a reference answer, 24 per intent, 75 zh-TW / 45 en.
- The planted contamination copies acceptance prompts, as before, and Silver drops them.

## Deploy and smoke

`10_lakehouse.py` (jobs and modules), `07_lambdas.py` (driver, state machine), `05_harnesses.py`
(data-prep, eval). Demo smoke **25/25 PASS** on v2:
- dataset digest `40f335d6…`, identical from the Glue job (Python 3.9) and locally (3.12);
- 8/8 plants exact;
- **acceptance set exported with all 120 items, reproduced byte for byte by the second export**;
- train/eval 257 / 26.

## Live run 1: `lakehouse_ingest` (`run-20261015T102914Z-e072d2e7`): SUCCEEDED in 11 min

Generate 57 s → Land 2 min 56 s → Silver 4 min 10 s → Gold 2 min 59 s → Complete.

## Live run 2: `lakehouse` (`run-20261015T104050Z-c7a2c9d8`)

The run trained on that dataset, student `Qwen/Qwen3-1.7B`, gates
`{format_validity: 0.95, answer_match: 0.6}` (owner-chosen).

- **Review → driver attestations:**
  - `gold_extract`: verified, 257 rows, `10264319…`;
  - `acceptance_set`: verified, 120 rows, `72253cab…`.

  The review recorded 9 checks passed, including that no acceptance prompt appears as a training user
  turn.
- **Training:** probe, then the full job `…-i0`, both Completed. **Eval** ran inference on the 120
  acceptance prompts (`…-evalinf-…-i0`) and scored them against the references in the file. Its
  report names the set and its sha256.

| Gate | Score | Wilson 95% CI | Threshold | Verdict |
|---|---|---|---|---|
| `answer_match` | **0.70** (84/120) | [0.613, 0.775] | 0.6 | decisive pass |
| `format_validity` | **0.983** (118/120) | [0.941, 0.995] | 0.95 | **borderline** (lower bound 0.941) |

Safety spot-checks passed: 0 PII leaks, 0 toxicity flags. ROUGE-L against the references averages 0.885.

The borderline protocol paged the owner. After 6 unanswered waiting turns the agent escalated
instead of looping remediation, and the run ended `FAILED` without a deploy. That is the protocol
working: the decision belongs to the owner, and it is recorded below.

### What the 36 misses and 2 format failures are

- **The format failures:** both are zh-TW `return_request` answers in which the student emitted a
  literal `[ORDERID]` token. That token appears nowhere in the data. It is the *style* of Silver's
  redaction tokens, which **27 of the 257 training rows** carry (`[NAME]` 6, `[EMAIL]` 6, `[TW_ID]` 6,
  `[PHONE]` 5, `[CARD]` 4), generalised into a token of the student's own. Redacted text is still
  training text, and the model learns its shape.
- **Some misses are scorer strictness.** The scorer is `deterministic_reference_match`. "The
  complaint on order TW10231 is logged; a supervisor will reply within a day." counts as a miss
  because the reference opens with "We are very sorry." Others are real generalisation errors: a zh
  complaint answered with the return policy, or a promo answer that drops the product. Misses by
  intent: promo 13, complaint 12, return 11; order status and stock check had none.
- **Every answer carries a case number**, because the generator appends one to every training answer.

So 0.70 is a conservative held-out score. It is the first gate number on this platform that says
something about the model rather than the plumbing.

## Found live: a job that finished before its token was parked was never resumed

`FinetuneLaunch` stalled after its training had finished:
1. The agent called `job_launched` naming the **probe**, kept working (polling, then launching the
   full job), and ran out its 840 s turn.
2. The driver services `job_launched` when the turn ends, so the token was parked at 11:05:44.
3. The probe had completed at 11:01:51. Its state-change event found no token and was skipped,
   correctly for that moment.
4. The full job's event (11:16) found no row tracking it.

Nothing would ever settle the token: the stage would sit until its 86400 s timeout. The same holds
for any launch-and-release handoff serviced after its job is terminal.

**The fix:** `_deliver_if_already_terminal` in the driver. After parking, the driver describes the
job, and if it is already terminal it hands the resume Lambda the same state-change event, so the
settle runs through the resume code (capacity stops, failures, the clear) rather than a copy of it.
A double delivery is the existing path: the first clears the token, the second finds none.
- **New grant:** `LateJobDelivery`, `sagemaker:DescribeTrainingJob` on `training-job/llmops-*` and
  invoke on the resume Lambda only. Simulated as `llmops-lambda-driver` 4/4, including NOT
  `StopTrainingJob` and NOT invoking another function.
- **A defect found while writing it:** SageMaker's describe field is `BillableTimeInSeconds`, not the
  event's `BillingSecondsUsed`. The first draft read a field that does not exist, which would have
  turned every late-delivered genuine stop into a "free" capacity relaunch. A test now holds the
  driver's event to the resume handler's own `_is_capacity_stop`.
- **The stuck run was released by one manual call of that same function** with real clients, after
  the fix was deployed. It sent the exact payload the fix would have sent had it been live at 11:05.
  The token was cleared, `last_job_status` is `Completed`, and the run moved to `FinetuneAnalyze`.

## Owner decision, the data-layer fix, and the retrain: **gate passed**

The owner ruled the borderline format gate a **failure** and asked for the placeholder leak to be fixed
at the data layer, then a retrain.

**The fix.** Silver keeps every redacted pair: it is the governed, audited record of what redaction
did. Gold, where training data is chosen, now promotes **no pair that still carries a redaction
token** (`transform.carries_redaction`), and `GOLD_DQDL` gained a rule asserting that no
`[CARD]`/`[EMAIL]`/`[NAME]`/`[PHONE]`/`[TW_ID]`/`[UBN]` appears in Gold instructions or outputs. That rule
uses LIKE over the exact token list, because Spark's LIKE treats brackets literally. The demo smoke
passed 25/25, Gold DQ included.

**Fresh ingest** (`run-20261015T125141Z-bcb61189`, SUCCEEDED): of 283 Silver pairs, **30 redacted pairs
were held back** from Gold. Train/eval is 230/23, and Gold DQ passed 12/12.

**First retrain** (`run-20261015T130703Z-f5d05255`): FAILED for a reason unrelated to the data.
- **Cause.** The full job sat in the ml.g5.2xlarge capacity queue past `FinetuneAnalyze`'s 3600 s
  ceiling. Under the probe → full protocol the full job is launched from analyze, which has no
  handoff, so analyze waited and timed out.
- **Fix.** `FinetuneAnalyze` moved to the 86400 s class: the one-hour ceiling protects states that
  hold an endpoint, and analyze holds none.
- **Follow-up.** The protocol itself is filed as its own task.
- **The orphaned job** was stopped before it started: 0 billable seconds.

**Retrain** (`run-20261015T142928Z-fbcd65dc`): SUCCEEDED, through the gated path. The probe waited
about 8 hours for capacity (22:32 → 06:45); after that the run went
`FinetuneAnalyze → EvalGenerate → EvalScore → EvalGate → QualityGateChoice → Deploy → SmokeTest →
MonitorHealth → Teardown → MonitorReport → Complete`.
- **Driver attestations:**
  - `gold_extract`: verified, 230 rows;
  - `acceptance_set`: verified, 120 rows.
- **The gate**, on the 120 held-out acceptance items:

| Gate | Before the fix (257 train rows) | After the fix (230 train rows) | Threshold | Verdict |
|---|---|---|---|---|
| `format_validity` | 0.983 (118/120), CI [0.941, 0.995] | **1.000 (120/120), CI [0.969, 1.000]** | 0.95 | **pass** |
| `answer_match` | 0.700 (84/120), CI [0.613, 0.775] | **0.725 (87/120), CI [0.639, 0.797]** | 0.6 | **pass** |

- **What changed.** Zero format failures: no coined placeholder token in any answer. That is the
  defect the fix targeted, gone. `answer_match` held up and rose slightly, even with 27 fewer
  training rows.
- **Remaining misses (33).** They are concentrated in promo (15) and complaint (11); return_request
  (4), stock_check (3) and order_status (0) have few or none. The scorer is still strict
  reference matching.
- **Serving, then teardown:**
  - **Smoke** passed 3/3 canaries, at p50 latency 473 ms.
  - **Registration:** the model package is `llmops-store-copilot-lakehouse-student/1` at
    **`PendingManualApproval`**. A passing gate is not an approval: that stays a human decision.
  - **Teardown:** the endpoint is deleted, with 2 endpoint configs and 2 models removed and
    `orphans_remaining: 0`. `list-endpoints` shows nothing for the run.

This is the first run on this platform to produce `gate_passed: true` and reach `Deploy` through
`QualityGateChoice`. ARCHITECTURE's 2026-08-15 measurement had found that no run had ever done so.

## Not done in this PR (from the Phase 1 plan)

- Starting the runs from a console task: the console has no intake for the two lakehouse modes yet.
- Tearing the lakehouse down under the demo posture: that is destructive, and waits on the owner.
