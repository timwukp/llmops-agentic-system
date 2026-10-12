# Cost — estimation, the approval gate, and reconciliation

**中文版：[COST.zh-TW.md](COST.zh-TW.md)**

This pipeline spends real money without a human in the loop: SageMaker GPU hours, Bedrock
teacher tokens, AgentCore runtime and memory, and the small services that carry them. Until
v1.1.0 nothing in the repo estimated that spend before a run, nothing reconciled it after,
and nothing could stop an expensive run. This document describes what was added, and — more
importantly — the ways a cost figure can be wrong, because every design decision here follows
from one of them.

**The premise: an estimate is a guess, but an *actual* must never be a guess.** A
confidently-wrong cost number is worse than an admitted unknown, because somebody approves
real spend on it. Every figure the system reports therefore carries where it came from and
whether it has settled.

---

## 1. The worked example: $10.77

Everything below is calibrated against one run whose cost is known from the bill rather than
from a model.

On 2026-07-31 a QLoRA fine-tune processed **16,550 rows** on one **ml.g5.2xlarge** in
**24,924 seconds** and billed **$10.77**.

```
Rate (Price List API, us-east-1, ml.g5.2xlarge training):  $1.515 / hour
Billable time:                                             24,924 s  ->  6.923 h
                                                           6.923 × 1.515 = $10.49
Plus the ~670 s of setup/teardown SageMaker also bills:    7.109 h × 1.515 = $10.77
```

The estimator reproduces this from the plan alone:

```
$ estimate_run({"sample_count": 16550, "train_rows": 16550, "endpoint_hours": 0}, card)

category:  sagemaker_training
quantity:  7.109444 hours
basis:     "(16,550 rows / 0.664 rows/s + 670 s setup) / 3600"
cost_usd:  10.770808          measured: $10.77          delta: 0.0%
```

The throughput constant (**0.664 rows/s**) and the setup overhead (**670 s**) are not guesses:
they are 16,550 ÷ 24,924 and the residual from that same run. This is the whole reason the
worked example matters — the model is *seeded from a measurement*, so its accuracy on the
training line is a fact, not a hope.

`tests/test_cost_model.py::test_estimator_reproduces_the_measured_1077_run`
asserts this delta stays under 1% (±$0.05 on $10.77). Per the plan's own rule, **a >20% miss on a run whose
actual is known is a model bug, not noise.**

### The other half of the same estimate

That same call also returns:

```
unpriced: ['agentcore:memory:short-term-events',
           'agentcore:runtime:gb-hours',
           'agentcore:runtime:vcpu-hours',
           'bedrock:global.anthropic.claude-fable-5:input-tokens',
           'bedrock:global.anthropic.claude-fable-5:output-tokens',
           'bedrock:us.deepseek.r1-v1:0:input-tokens',
           'bedrock:us.deepseek.r1-v1:0:output-tokens']
```

Seven SKUs have no rate, so they contribute **$0** to the total. The total is therefore a
**floor, not an estimate** — and the estimate says so, in `assumptions`, on screen, and in
`unpriced[]`. A `$0` line from a missing rate and a `$0` line from a free tier are otherwise
indistinguishable, which is exactly how a teacher model silently prices at nothing.

### The retrieval index (RAFT runs): the one standing cost

A RAFT plan (r6d+) carries `kb_ocu_hours` and `kb_embed_ingest_tokens`, and the estimate
gains a `retrieval_index` category: OCU-hours at the AOSS rate (fallback_static **$0.24/OCU-hr**
— pinned by test to `deploy/09_retrieval.py`'s constant — until `pricing_refresh` sees the
bill) plus the one-time embedding of the corpus at ingest. Two properties are load-bearing:

- **It is a STANDING cost.** The collection bills its OCUs (2-OCU floor ≈ **$11.52/day**)
  while it *exists*, queried or not. The estimate prices exactly the hours the plan admits
  to, and its assumption names the only thing that stops the meter:
  `deploy/09_retrieval.py --teardown`, run when the eval is done.
- **It is NOT remediable.** A remediation iteration re-trains and re-judges; it does not
  re-provision the index, so `worst_case_usd` adds the retrieval subtotal once, never
  times `max_iterations`.

On a closed-book plan the category is absent entirely — a `$0` retrieval line would read
as "priced and free", the same confusion the unpriced-vs-zero rule above exists to prevent.

---

## 2. Where rates come from, and why the obvious source is not enough

| Source | Precedence | Verified behaviour |
|---|---|---|
| `ce_realized` — our own bill: unit price = cost ÷ quantity | **1st** | Yields `USE1-DeepSeek-R1-input-tokens` at **$0.00135/1K** and output at **$0.0054/1K** |
| `price_list` — AWS Price List API | 2nd | Gives ml.g5.2xlarge at **$1.515/h** exactly, and DeepSeek-R1 to within **<0.001%** of our realized rate. But every `AmazonBedrock` entry for `provider=Anthropic` is Claude 3 or older |
| `fallback_static` | 3rd | Hand-entered, always labelled as such |

**The Price List API cannot price the harness fleet's own models.** Queried live on
2026-07-31, every `provider=Anthropic` entry for us-east-1 is `Claude 2.0 · Claude 2.1 ·
Claude 3 Haiku · Claude 3 Sonnet · Claude Instant` — no Fable 5, no Opus 5. Those are the
models the seven harnesses themselves run on, and the largest AgentCore line in the bill. A
pricing refresh built only on Price List would return "success" and silently price the whole
agent fleet at $0.

It *can* price the teacher: DeepSeek-R1 comes back at $0.00135/$0.0054 per 1K, matching our
realized rate to **<0.001%**. An earlier version of this document said otherwise, and the
reason it was wrong is worth keeping: the `model` attribute value is bare **`R1`** (with
`provider=DeepSeek`), so reading the 84 model values looking for a name containing
"DeepSeek-R1" finds nothing and concludes absence. **Query with an explicit
`Field=model,Value=R1` filter, not by eye.** Coverage is re-probed on every refresh, because
which models the API carries is exactly what changes between refreshes.

Hence the precedence: **realized billing outranks the published price list**, because our own
invoice is the only source guaranteed to cover what we actually use. Price List is the
fallback for resources never yet used, where no realized rate can exist.

Rates are cached at `s3://<bucket>/finops/rates/rate_card_latest.json` plus a dated history.
The history is not housekeeping: an estimate stamps `rate_card_as_of`, so a variance
questioned months later can be re-derived against the rates that were live when the estimate
was made. Storing only "latest" makes an old miss unexplainable — the estimate looks wrong
when in fact the rate card moved.

### Rate card health is measured against what a plan needs

`rate_card_health(plan)` reports missing rates relative to `required_skus_for(plan)`, not
relative to the card's own contents. Forty irrelevant rates and no teacher price is **not**
a healthy card, and a check that counts rows would call it healthy.

---

## 3. The $20,000 reference (advisory)

Two figures, both recorded in **every KMS-signed task approval** and neither of which blocks:

| Reference | Default | What it measures |
|---|---|---|
| Single-run | `APPROVAL_LIMIT_USD` = **$20,000** | the plan's `cost_estimate_usd`, read as its worst case |
| Cumulative | `CUMULATIVE_LIMIT_USD` = **$20,000** | project-to-date actual **+** this plan |

Twenty $1,500 runs are the same exposure as one $30,000 run, and each of the twenty passes a
single-run comparison on its own — which is why both are recorded. When either is crossed, the
signed record's `gate.over_budget` names the overage; the plan still dispatches.

**Enforcement is not here.** The one way to refuse a launch over budget is the Cost tab's visible
*Block launches over budget* switch (§8), evaluated against the budgets an operator defines. The
earlier `BUDGET_MODE=blocking` path and the approval queue it fed were removed with the v1 cost
routes on 2026-10-12: a second, env-var-only way to refuse a launch is a control nobody can see
from the portal.

### Why $20,000, and why a number at all

Raised from $2,000 on 2026-08-02 by the platform owner's instruction. This is the project's own
design-and-test platform, not a customer's production account: the whole test-proven record —
six agents, a trained model, a deployed and torn-down endpoint, five e2e iterations — cost
**~$12–15**. A reference low enough to be crossed by ordinary work is a reference that gets
ignored.

Keeping a number at all is the less obvious half. The reference stops nothing, so its only job
is to make an over-estimate plan get **named** in the record a third party audits. Delete the
number and that line goes with it. A high reference still reports; no reference reports nothing.

The number lives in exactly one place that decides — `cost_model.DEFAULT_SINGLE_RUN_LIMIT_USD`.
`deploy.sh` reads it out of the bundled module to set `APPROVAL_LIMIT_USD`, and the console's own
fallback literal is pinned equal to it by
`test_console_cost.py::test_the_consoles_fallback_limits_equal_the_canonical_ones`.

### Unknown is a third answer

| Failure | Behaviour | Why not the alternative |
|---|---|---|
| `cost_model` import fails | recorded as `budget_unknown`, with a note that the plan was **not** checked | "We could not check" must never render as "under the reference" |
| Cognito group lookup fails | **deny** | A throttled API call must not become an authorization |

### Who may enforce

Flipping an enforcement switch requires membership in the Cognito group `llmops-approver`,
checked **server-side on every call** — hiding a button is not a control. Every flip is written
to the alert feed with who and when.

Two facts make this harder than it looks: `cognito-idp:GetUser` validates the token and returns
the username but **not** group membership, and a bearer *access* token carries no
`cognito:groups` claim. Membership therefore needs a second call (`AdminListGroupsForUser`) —
which can fail, and when it does the user gets an empty group list and is denied.

### 401 and 403 are different in the browser

`401` means the token is gone; `403` means the token is fine but this user lacks the right. The
console keeps them apart. Collapsing them would sign an approver out of a working session and
hide "you are not in the approver group" behind "session expired".

---

## 4. Attribution: by resource, never by service

This is the single most consequential decision in the design, and it comes from a measurement.

On 2026-07-31, this AWS account's month-to-date total was **$27,491**. This project's own
share was **~$3.50 of SageMaker plus ~$6.29 of Bedrock teacher — about $10–15.** The rest
belongs to unrelated work in the same account, including SageMaker Canvas session hours
(~$296) and a JumpStart Whisper endpoint (~$36.36/day).

A rollup that filtered by *service* would therefore report **thousands of dollars of somebody
else's spend as ours** — and would trip the $2000 gate on its first evaluation. Attribution is
allowlist-based: an explicit match against resources this project created.

```
ce get-cost-and-usage-with-resources  grouped by RESOURCE_ID
  -> training-job/llmops-qlora-run-phase2-main-0001-r3   $0.1035
  -> endpoint/llmops-student-run-phase2-main-0001-v5     $1.0203
```

Per-run attribution needs **no tagging at all**, because `run_id` is already inside the job
and endpoint names. That matters, because the tag path does not work:

- `ce list-cost-allocation-tags` shows `project` and `Project` both **Inactive**; zero Active
  tags on the account.
- A CE query filtered on `Tags project=llmops-agentic-system` returns **$0.00** for
  2026-07-30 — a day with real spend.

Cost allocation tags are also **not retroactive**: the $10.77 run will never carry one. The
tag is activated as a future cross-check, never as the primary attribution.

AgentCore token spend is attributed through CloudWatch `aws/spans`, where each span carries
`attributes.session.id` alongside `gen_ai.usage.input_tokens`/`output_tokens` — and the
console already builds `session_id` from `(run_id, stage, task)`. The CloudWatch *metric*
`bedrock-agentcore/gen_ai.client.token.usage` cannot be used for this: its dimensions are
`server.address`, `gen_ai.request.model`, `gen_ai.token.type` — no run, no session. It is an
account-wide number.

---

## 5. Settled vs provisional

Cost Explorer lags roughly **24 hours** and marks recent periods `Estimated: true`. A
resource-level query for 2026-07-31 run on 2026-07-31 returned `Estimated: true` and **zero
groups**.

Consequences, all enforced:

- Reconciliation is **asynchronous and re-runnable**, never inline with the run.
- Every actual row carries `settlement` ∈ `provisional | settled`.
- The rollup reports `settled_usd` and `provisional_usd` **separately** — never one blended
  total, because a blended total invites quoting a figure that has not landed.
- A **run** is settled only if **every** row for it is settled. One provisional row means the
  total can still move, and calling that settled is the error this rule exists to prevent.

---

## 6. Variance: which line missed, not by how much overall

`reconcile(estimate, actual)` returns per-category variance, an `accuracy_ratio`, and a
`verdict` that **names the driving category**. One aggregate "40% off" tells nobody what to
fix; "bedrock_teacher drove the miss" does.

`reconcile` runs in the `llmops_finops` auditor (§7). In the console, estimate vs actual is
the **By task** sub-tab (§8): each task's signed `cost_estimate_usd` against the attributed
spend of the runs it dispatched. The v1 console variance panel, which joined estimates to
actuals rows, was removed on 2026-10-12 along with the estimator that produced those estimates.

---

## 7. The `llmops_finops` runtime

A 7th AgentCore harness — 財務審計員／統計員／報告員 (auditor / accountant / reporter) —
running three duties keyed on `params.task`:

| Task | Trigger | Does |
|---|---|---|
| `reconcile` | daily 09:00 UTC, or on demand | CE resource-level + spans → per-run actuals, variance vs estimate |
| `pricing_refresh` | on demand | realized rates (primary) + Price List (fallback) → rate card |
| `report` | on demand | project rollup + estimate-accuracy trend |

### Why a 7th runtime instead of extending `monitor`

`monitor` runs **inside** the state machine: per-run, within a run's lifetime, answering "is
the endpoint alive now". Reconciliation is the opposite shape — it runs **after** the run is
over (CE lag), spans **many** runs, and answers to the project rather than the run. A run that
finished yesterday has no live agent to attribute today's settled bill. So `llmops_finops`
sits beside `llmops_orchestrator`, above the state machine: the conductor decides *what to
spend*, the auditor reports *what was spent*.

It appears in the console's fleet view but never in a run's stage sequence.

### The auditor cannot stop a run

Its IAM is **read-only on billing** — `ce:Get*`, `pricing:*`, `budgets:ViewBudget` — and
it has no authority to terminate anything. The auditor must never be able to change what it
audits, and spend-control authority belongs to the orchestrator (via `page_human`), not to the
component whose job is to observe. An auditor with kill rights is a different and riskier
design.

---

## 8. The Cost tab

### Cost v2: design (2026-10-12)

The tab answers two questions, in this order: **where did the money go** (visibility) and
**what happens when it goes too far** (governance). Six sub-tabs:

| Sub-tab | Shows | Basis |
|---|---|---|
| Overview | workload spend, month to date, attributed training, Cost Explorer billed; daily stacked chart; top spenders | attributed + estimate; billed shown beside, never summed in |
| Usage | Bedrock tokens and cost per model; training instance-hours | account-wide (Bedrock has no per-harness token dimension) |
| By task | per console Task: estimate vs actual of the runs it dispatched, variance | attributed |
| Breakdown | by category, model, run, AWS service | mixed, each badged |
| Detail | every attributed training job: instance, hours, unit price, cost; rate-card health with *Refresh rates* / *Reconcile now* | attributed |
| Governance | enforcement policy (two switches), budgets, forecast, guardrail posture, alert feed, AWS Budgets | estimate vs limit |

#### Where the numbers come from

Live, `llmops-cost-actuals` held 164 rows and every one was `#audit#` — the reconcile had
never written an attributed cost row, so v1 read $0.00 while 68 training jobs had run. v2
therefore prices what the pipeline demonstrably left behind:

- **Training (attributed):** jobs whose names start with `COST_JOB_PREFIXES`
  (`llmops-,arc2v2-`; never "all SageMaker" — the account runs unrelated jobs), billable
  seconds × the same S3 rate card the estimator uses. A job still `InProgress` is priced by
  elapsed time, so the sweep sees spend accrue while it runs. The run id is parsed from the
  job name; a task's actual is the sum over the runs it dispatched.
- **Model tokens (estimate, account-wide):** CloudWatch `AWS/Bedrock` Input/OutputTokenCount
  by `ModelId` × the rate card. It is context for what the agents cost, not attributed to a
  task.
- **Billed (account-wide):** Cost Explorer daily unblended for the workload's services,
  cached 15 minutes because each request bills $0.01, and ~24 h behind.

#### Governance: budgets, then one visible policy

A **budget** is a limit and its thresholds (default 50 / 80 / 100 %), scoped either
`project_monthly` (month-to-date spend) or `per_task` (one launch estimate, or one running
run's spend). Budgets carry no enforcement setting of their own. Enforcement is **one
policy** shown at the top of the Governance sub-tab, so anyone looking at the screen can see
what a breach will do before it happens:

| Control | Default | When ON |
|---|---|---|
| Alarm + notification | **always ON** | — every breach writes an alert row and publishes to SNS `llmops-escalations` |
| Block launches over budget | **OFF** | `POST /api/start-run` and `POST /api/tasks/{id}/accept` return **409** naming the budget |
| Stop running jobs over budget | **OFF** | the sweep stops the breached run's Step Functions execution and its in-progress training jobs |

With both switches OFF — the deployed state — a breach is an alarm and nothing is refused or
stopped. That is a deliberate default, not a missing feature: the switches exist, are visible,
and an approver turns them ON when a budget must be enforced.

- **Who may flip a switch:** members of `llmops-approver` only (`403` otherwise). Every flip
  is written to the alert feed with who and when, so arming a control is itself on the record.
- **When it is checked:** at every launch (the gate), and every 15 minutes by the EventBridge
  rule `llmops-budget-sweep`, which invokes the console Lambda with `{"mode":
  "budget-sweep"}`. The portal's **Run budget check now** button runs the same sweep on demand.
- **Alarms do not repeat:** the sweep writes one alert per budget, threshold and month
  (`budget-alert#<YYYY-MM>#<budget>#<threshold>`), so a breached budget does not page every
  15 minutes.
- **What "stop" stops:** `states:StopExecution` on `llmops-pipeline` executions and
  `sagemaker:StopTrainingJob` on `llmops-*` / `arc2v2-*` jobs — nothing else. Endpoints are
  deliberately left to the pipeline's Teardown stage; `DeleteEndpoint` is not reversible and is
  not granted. The IAM grant (`CostGuardrailStop`) exists whether or not the switch is ON: the
  switch is the policy, IAM is the ceiling.
- **Notification never gates the decision:** the alert row and the SNS publish are wrapped
  separately, so a failed notification cannot change whether a launch is refused or a run is
  stopped.

#### API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/cost/v2?days=N` | public read | all five visibility sub-tabs in one payload |
| GET | `/api/cost/budgets` | public read | budgets with state, policy, posture, alerts, AWS Budgets |
| POST | `/api/cost/budgets` | authed | create or update a budget |
| POST | `/api/cost/policy` | authed **+ `llmops-approver`** | flip `block_launches` / `stop_running` |
| POST | `/api/cost/sweep` | authed | run the budget sweep now |
| POST | `/api/finops-run` | authed | trigger the auditor's `reconcile` / `pricing_refresh` / `report` |

#### Removed: the v1 panels and routes (2026-10-12)

The v1 tab had five panels — an estimator, an approval queue, an actuals rollup, a variance
table and rate-card health — behind five routes: `GET /api/cost-overview`,
`GET /api/cost-estimates`, `POST /api/cost-estimate`, `POST /api/cost-approval-request` and
`POST /api/cost-approval`. All five routes and the first four panels are gone:

| v1 | Replaced by |
|---|---|
| actuals rollup (read `llmops-cost-actuals`, which held no cost rows) | Overview / Breakdown / Detail, priced from real usage |
| estimate vs actual | By task |
| approval queue + `BUDGET_MODE=blocking` | the *Block launches over budget* switch |
| line-item estimator | the orchestrator prices every plan from the same rate card (`rate_card_for_prompt`); the signed `cost_estimate_usd` is what By task compares |

Rate-card health survived and moved to Detail, because every v2 figure is priced from that card;
an unnoticed stale rate makes all of them confidently wrong. `/api/finops-run` survived with it.

The pricing arithmetic still lives in **one** place — `pipeline/contracts/cost_model.py` — which
the auditor, the advisory reference and the rate-card health check all delegate to.

---

## 9. Storage

| Table | Key | Holds |
|---|---|---|
| `llmops-cost-estimates` | PK `id` | one `est-task-*` row per accepted task (`worst_case_usd` = its signed `cost_estimate_usd`, `status` `launched`), read by the reconcile Lambda; rows from the removed v1 estimator remain as history |
| `LlmopsAdminRuns` (console) | PK `id` | Cost v2 budgets `budget#*`, the enforcement policy `cost-policy`, alerts `budget-alert#*` |
| `llmops-cost-actuals` | PK `project`, SK `<period>#<run_id>#<category>` | attributed cost rows with `settlement` and `ce_estimated_flag`; plus reserved `#audit#` and `#finding#` rows |

The signed approval itself lives in S3 (`tasks/<id>/approval.json`) and on the task row, not in
either cost table: it is an audit record, KMS-signed and hash-chained.

`#audit#` and `#finding#` rows are **excluded from every cost sum**. They are the agent's own
notes — a finding describes a variance, and summing it would double-count the very spend it
describes.

---

## 10. What this does not do

- **No multi-account or Organizations rollup** — single account today.
- **No Savings Plans or Reserved Instance modelling** — on-demand rates only, stated as an
  assumption in every estimate.
- **No auto-stop on budget breach by default** — the auditor reports and flags (§7), and the
  console's *Stop running jobs over budget* switch, which can stop a breached run, ships OFF.
  It stops executions and training jobs only, never endpoints. See §8.
- **The total is a floor whenever `unpriced[]` is non-empty.** Today that includes the teacher
  and harness token lines, which is not a small omission — it is stated on every estimate
  rather than hidden.

An account-level guardrail already exists below this system's gate: one AWS Budget,
`bedrock-monthly-dev`, at **$1000/month**. The Cost tab surfaces it rather than duplicating it.

**That budget is filtered to `Service: ["Amazon Bedrock"]`** (`describe_budgets`, 2026-08-02) —
which is worth stating in both directions, because the filter is simultaneously what keeps the
guardrail meaningful and what makes it blind:

- It meant the `jumpstart-dft-hf-asr-whisper-large-v2` orphan (§4, $36.36/day = **~$1106/month**
  at 730 h × $1.515/hr, deleted 2026-08-02) never consumed our Bedrock headroom. An unfiltered
  $1000 budget would have been silently 100%+ eaten by a resource that predates this project,
  and the first real Bedrock spend would have tripped an alarm that had nothing to do with
  Bedrock.
- It ALSO meant **no account-level control would ever have flagged that endpoint.** A budget
  scoped to one service cannot notice waste in another. What found it was the monitor sweep, which
  scans the whole account precisely because a check restricted to what we already call ours cannot
  find something unclaimed. The two controls are not redundant; the budget bounds *our* spend and
  the sweep looks for spend nobody has claimed.

---

## 11. Tests

| File | Tests | Covers |
|---|---|---|
| `tests/test_cost_model.py` | 71 | estimate arithmetic, rate precedence, attribution, reconcile |
| `tests/test_finops.py` | 79 | harness config, reconcile Lambda, storage, IAM shape |
| `tests/test_console_cost.py` | 25 | HTTP layer, the advisory reference, rate-card health, finops dispatch, v1 removal |
| `tests/test_console_cost_v2.py` | 21 | enforcement policy, launch gate, budget sweep, stop path, IAM scope |

All 196 run without AWS credentials, against injected clients and account `123456789012`.

The suite was **mutation-checked**: each guard was broken in turn and the run re-executed to
confirm a test fails. That found two guards a green suite did not cover — a launch-time gate
reading `total_usd` instead of `worst_case_usd`, and a terminal-status check that was never
exercised on under-limit estimates. Both now have tests. A test that cannot fail is not a
test, and only mutation shows which ones those are.
