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

`tests/test_console_cost.py::test_estimate_matches_the_measured_e3_run_within_one_percent`
asserts this delta stays under 1%. Per the plan's own rule, **a >20% miss on a run whose
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

## 3. The $2000 gate

Approval fires when **either** limit is crossed:

| Limit | Default | Rationale |
|---|---|---|
| Single-run | `APPROVAL_LIMIT_USD` = **$2000** | One expensive run |
| Cumulative | `CUMULATIVE_LIMIT_USD` = **$2000** | Project-to-date actual **+** this estimate |

Twenty $150 runs are the same exposure as one $3000 run, and each of the twenty passes a
single-run check on its own. A gate with only the first limit is a gate against one shape of
overspend and blind to the other.

### It gates on `worst_case_usd`, never `total_usd`

The remediation loop can re-run finetune up to `max_iterations` (default 3). So an estimate
has two numbers:

```
sample_count 2,000,000, max_iterations 3:
    total_usd       $1,268.32     (expected — one pass)
    worst_case_usd  $3,803.95     (all three remediation iterations run)
```

Gating on `total_usd` would wave this through at $1,268 and permit $3,804 of spend. Approving
$2000 that can silently become $6000 is not a gate. The estimate reports both; the gate reads
the worst case, and `gating_basis` in the response says which field decided.

### The gate is re-derived at launch, not trusted from storage

An estimate priced when project-to-date was low is a different exposure a week later. So
`start_run` recomputes the decision at launch and requires approval if **either** the stored
verdict or the fresh one demands it. A gate that trusts a stale verdict is no gate.

### Every failure path fails closed

| Failure | Behaviour | Why not the alternative |
|---|---|---|
| `cost_model` import fails | approval **required** | "We could not check the limit" must land on the require-approval side, never the allow side |
| No rate card | estimate **refused (503)** | A `$0`-with-warnings total gets quoted; an explicit refusal does not |
| Cognito group lookup fails | **deny** | A throttled API call must not become an approval |
| No `sample_count`/`train_rows` | **400** | With neither, the training line is $0 and the total is not an estimate |

### Separation of duties

Approval requires membership in the Cognito group `llmops-approver`, checked **server-side on
every call** — hiding the button in the UI is not a control. The submitter cannot approve
their own request: self-approval is **rejected with 403**, not merely flagged.

Two facts make this harder than it looks: `cognito-idp:GetUser` validates the token and
returns the username but **not** group membership, and a bearer *access* token carries no
`cognito:groups` claim. Membership therefore needs a second call
(`AdminListGroupsForUser`) — which can fail, and when it does the user gets an empty group
list and is denied.

Terminal states are terminal. A `rejected` estimate cannot be re-launched (otherwise a
rejection can be quietly retried until someone approves it) and a `launched` one cannot launch
again (otherwise two runs attach to one approval and double-count in the variance report).
Both apply whether or not the estimate needed approval — a cheap run is the one nobody thinks
to guard.

### 401 and 403 are different in the browser

`401` means the token is gone; `403` means the token is fine but this user lacks the right.
The console keeps them apart. Collapsing them would sign an approver out of a working session
and hide "you are not in the approver group" behind "session expired".

---

## 4. Attribution: by resource, never by service

This is the single most consequential decision in the design, and it comes from a measurement.

On 2026-07-31, this AWS account's month-to-date total was **$27,491**. This project's own
share was **~$3.50 of SageMaker plus ~$6.29 of Bedrock teacher — about $10–15.** The rest
belongs to unrelated work in the same account, including SageMaker Canvas session hours
(~$296) and a JumpStart Whisper endpoint (~$18/day).

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

The variance report also states `n_unestimated` — how many runs had no estimate at all. Runs
launched without an estimate stay legal, because that is how every run worked before v1.1.0,
and keeping it legal is what lets the report say honestly what fraction of spend was never
estimated. A variance report silent about that implies a coverage it does not have.

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

Its IAM is **read-only on billing** — `ce:Get*`, `pricing:*`, `budgets:DescribeBudgets` — and
it has no authority to terminate anything. The auditor must never be able to change what it
audits, and spend-control authority belongs to the orchestrator (via `page_human`), not to the
component whose job is to observe. An auditor with kill rights is a different and riskier
design.

---

## 8. The Cost tab

Five panels, in the money's own order:

1. **Estimate a run** — plan inputs → a line-item table with `basis` and `rate_source` per
   row, `total` vs `worst_case`, and an explicit red UNPRICED block.
2. **Approval queue** — pending requests with full estimate detail; reject requires a reason.
3. **Actual spend by project, itemised** — by category, by service, by run, by period, with
   `settled`/`provisional` badges.
4. **Estimate vs actual** — per-run variance, driving category, and `n_unestimated`.
5. **Rate card health** — count, sources, oldest `as_of`, and what a plan needs but the card
   lacks.

The fifth panel is not an afterthought. An unnoticed stale rate makes the other four
confidently wrong — it is the panel that would have caught DeepSeek-R1 pricing at $0.

### API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/cost-overview` | public read | rollup, settled/provisional split, budgets, rate-card health |
| GET | `/api/cost-estimates` | public read | estimates, approval queue, variance |
| POST | `/api/cost-estimate` | authed | price a draft plan |
| POST | `/api/cost-approval-request` | authed | submit for approval |
| POST | `/api/cost-approval` | authed **+ `llmops-approver`** | approve / reject, optionally launch |
| POST | `/api/finops-run` | authed | trigger `reconcile` / `pricing_refresh` / `report` |

The gate arithmetic itself lives in **one** place — `pipeline/contracts/cost_model.py`. The
console delegates to it rather than reimplementing it, because a second copy is the copy that
drifts, and the drifting one would be the one guarding the launch button.

---

## 9. Storage

| Table | Key | Holds |
|---|---|---|
| `llmops-cost-estimates` | PK `id` | the full line-item estimate, `worst_case_usd`, `status` ∈ `draft｜pending_approval｜approved｜rejected｜launched｜reconciled`, `requested_by`, `approved_by`, `decided_at`, `rejection_reason`, `sfn_execution_arn`, `rate_card_as_of` |
| `llmops-cost-actuals` | PK `project`, SK `<period>#<run_id>#<category>` | attributed cost rows with `settlement` and `ce_estimated_flag`; plus reserved `#audit#` and `#finding#` rows |

Approval state lives in its own table rather than the console's generic one: it is an audit
record and needs its own schema and retention story.

`#audit#` and `#finding#` rows are **excluded from every cost sum**. They are the agent's own
notes — a finding describes a variance, and summing it would double-count the very spend it
describes.

---

## 10. What this does not do

- **No multi-account or Organizations rollup** — single account today.
- **No Savings Plans or Reserved Instance modelling** — on-demand rates only, stated as an
  assumption in every estimate.
- **No auto-stop on budget breach** — the auditor reports and flags; stopping a run is the
  orchestrator's authority. See §7.
- **The total is a floor whenever `unpriced[]` is non-empty.** Today that includes the teacher
  and harness token lines, which is not a small omission — it is stated on every estimate
  rather than hidden.

An account-level guardrail already exists below this system's gate: one AWS Budget,
`bedrock-monthly-dev`, at **$1000/month**. The Cost tab surfaces it rather than duplicating it.

---

## 11. Tests

| File | Tests | Covers |
|---|---|---|
| `tests/test_cost_model.py` | 52 | estimate arithmetic, rate precedence, attribution, reconcile |
| `tests/test_finops.py` | 36 | harness config, reconcile Lambda, storage, IAM shape |
| `tests/test_console_cost.py` | 59 | HTTP layer, the dual gate, separation of duties, launch guards |

All 147 run without AWS credentials, against injected clients and account `123456789012`.

The suite was **mutation-checked**: each guard was broken in turn and the run re-executed to
confirm a test fails. That found two guards a green suite did not cover — a launch-time gate
reading `total_usd` instead of `worst_case_usd`, and a terminal-status check that was never
exercised on under-limit estimates. Both now have tests. A test that cannot fail is not a
test, and only mutation shows which ones those are.
