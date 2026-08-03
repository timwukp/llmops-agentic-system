# FinOps verification — the 7th runtime, the rate card, and what failing taught us

Date: 2026-07-31 · Region: us-east-1 · Redacted per SECURITY.md.

## Gate

> `llmops_finops` live; rate card derived from our own bill; every number carries its
> provenance; the auditor cannot change what it audits; nothing publishes a guess

**Result: PARTIALLY PASSED.** The runtime is live and its arithmetic is proven. The rate
card is **not yet published** — blocked on an IAM apply, documented below. Every
intermediate failure is recorded here rather than smoothed over, because two of them are
the strongest evidence in this file that the design is correct.

## Live fleet — 7 harnesses READY

`llmops_data_prep`, `llmops_finetune`, `llmops_eval`, `llmops_deploy`, `llmops_monitor`,
`llmops_orchestrator`, **`llmops_finops`**.

- `llmops_finops` created via `deploy/05_harnesses.py --agent finops`, status READY.
- Memory attached by `deploy/04_wire_memory.py` to the **existing** shared store
  (`created: false` — no duplicate memory, verified in the response).
- `deploy/validate_config.py --config agents/finops/harness.json` → `RESULT: OK`.
- `llmops-finops-reconcile` Lambda created, invoked live: **200, no FunctionError**,
  handing off to the harness driver (202), under its **own** least-privilege role.

## Test suite

`.venv/bin/python -m pytest tests/ -q` → **274 passed**.

FinOps-specific: `tests/test_finops.py` (44) + `tests/test_cost_model.py`. Every guard
added in this work was mutation-checked — the asserted behaviour was reverted one at a
time and the test confirmed to fail. A test that passes both with and against the
behaviour it names is not a test.

## The two failures that verify the design

### 1. Denied billing reads → the auditor reported zero, not a plausible number

First `pricing_refresh`, running before `01_iam.py` had granted billing reads. Every
Cost Explorer and Price List call returned AccessDenied. The agent:

- reported **"Priced SKUs this period: 0. Unpriced: all"**
- stated the existing card's freshness as **unknown** rather than assuming it fresh
- **declined to call `update_rate_card`** — there was no card to advertise
- named the exact missing permissions

Fixed by `deploy/01_iam.py`, which added `BillingReadOnlyForFinOps`,
`CostLedgerForFinOps`, `CostActualsLedgerForFinOps` and created the
`finops_reconcile` Lambda role. All six requested permissions verified present before
re-invoking.

### 2. Denied S3 + no reachable module → 37 correct SKUs, published as nothing

Second `pricing_refresh` succeeded at the billing work and produced a **complete 37-SKU
rate card with `unpriced: []`** — then refused to publish it. Two blockers, both real:

- Every S3 verb denied on the project bucket, including the two mandated writes
  (`finops/rates/rate_card_latest.json` and the dated history copy).
- `cost_model.py` was reachable by no path at all, so `merge_rates`,
  `rate_card_health` and `update_rate_card` were never invoked.

The agent applied the merge precedence by hand, **stamped its own output
`rate_card/v1-DRAFT-noncanonical`**, and said in its first line that the card must not
replace `rate_card_latest.json` until regenerated through the module — because the
`fallback_static` tier lives *inside* the module it could not reach.

**This is the whole point.** The failure mode that would have mattered is a
confident-looking 37-SKU card that nobody could regenerate next month. Someone approves
a five-figure run on these figures. Fail-closed held under conditions nobody designed for.

## Rate card contents (derived, provisional, not yet published)

- **Source:** `ce get-cost-and-usage`, 2026-07-01→31, MONTHLY, grouped by USAGE_TYPE —
  956 usage types, 418 with cost > $0. Unit rate = `UnblendedCost ÷ UsageQuantity`, with
  the quotient and both operands recorded in each rate's `basis` field.
- **Cross-check:** the 5 SKUs priced by *both* CE-realized and Price List agree to
  **<0.001%** — realized rates are trustworthy as primary.
- **Settlement:** CE marks 2026-07 `Estimated: true`, so **every rate is PROVISIONAL**
  and the refresh must re-run after the period settles (~24 h lag).
- Key rates: DeepSeek-R1 **$0.00135/1K in, $0.0054/1K out**; ml.g5.2xlarge Train/Host
  **$1.515/hr** (the rate behind the measured $10.77); AgentCore Runtime
  $0.0895/vCPU-h + $0.00945/GB-h; Fable 5 $0.010/$0.055 per 1K.
- Price List filled exactly **one** SKU the bill has never seen: Train ml.g5.xlarge
  @ $1.408/hr.

## Correction to a planning finding

The plan recorded, as verified, that the Price List API **cannot price DeepSeek-R1**.
That is **wrong**, and the reason is worth keeping: the `model` attribute value is bare
**`R1`** (with `provider=DeepSeek`), so scanning 84 model values for a name containing
"DeepSeek-R1" finds nothing and concludes absence. Re-probed 2026-07-31 with an explicit
`Field=model,Value=R1` filter: R1 **is** priced, at $0.00135/$0.0054 — an exact match to
our realized rate.

The hazard the rule guards is nonetheless real, and **worse** than the plan stated. Every
Anthropic-provider entry in Price List for us-east-1:

```
Claude 2.0 · Claude 2.1 · Claude 3 Haiku · Claude 3 Sonnet · Claude Instant
```

No Fable 5, no Opus 5 — **the harness fleet's own LLM usage, the largest AgentCore
line**. A Price-List-only refresh silently zero-prices it. The prompt rule was rewritten
around what is actually unpriceable, and coverage is now re-probed every run: a hazard
rule resting on a false example gets deleted by the next reader who checks it.

## Four deployability defects found by deploying

Each was repo-complete, documented, and unit-tested — and would never have been created:

| Defect | How it presented |
|---|---|
| `agents/finops/harness.json` not in `05_harnesses.py`'s `AGENTS` | `--agent finops` rejected as an invalid choice; fleet stayed at 6 while docs said 7 |
| No `LAMBDAS` entry for `llmops-finops-reconcile` | `08_triggers.py` already scheduled it — a daily `ResourceNotFound` visible only in the scheduler's metrics |
| `update_function_configuration` never passed `Role` | Role change applied only to functions that don't exist yet; every re-run reported `"updated"` while the live function kept its birth role. **Measured**: stayed on `llmops-lambda-driver` across a successful `"updated"` run, moved only after `Role=` was added |
| Execution role had no `finops/*` or `contracts/*` S3 grant | The failure in §2 above |

The `ListBucket` asymmetry is the subtle one: a prefix granted for Get/Put but missing
from the condition can be read object-by-object and never **enumerated**, which presents
as *"the rate card history is not there"* when it is.

## Reproducible checks

```bash
.venv/bin/python -m pytest tests/ -q                                 # 274 passed
.venv/bin/python deploy/validate_config.py --config agents/finops/harness.json  # RESULT: OK
.venv/bin/python deploy/05_harnesses.py --region us-east-1 --dry-run  # names 7 agents
.venv/bin/python deploy/03_storage.py --region us-east-1 --account-id 123456789012 --dry-run
```

The last one prints `contracts: would upload 4 contract files` — the check that the
canonical module has a distribution path at all.

## Outstanding

1. **`deploy/01_iam.py` apply** (the `finops/*` + `contracts/*` S3 grant) — awaiting
   explicit authorization. Three things queue behind it: the contracts upload, the
   canonical rate card, and estimator validation.
2. **Estimator vs the $10.77 ground truth** (25,592 s × $1.515/h on ml.g5.2xlarge).
   A >20% miss on a run whose actual is known is a model bug, not noise. Needs the card.
3. **Re-reconcile after 2026-07 settles** — everything above is `provisional` by CE's own
   flag, and publishing it as settled is the one thing the prompt forbids.
4. **`llmops-finops-daily` schedule** — its target function now exists.
5. **Approval gate end-to-end on real Cognito**, walked in a browser with screenshots:
   `pending_approval` blocks launch; admin-only user gets 403; self-approval rejected;
   approver approve → SFN execution starts and the ARN is stamped.
