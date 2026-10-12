# VERIFICATION — Cost tab v2 (visibility + governance)

Date: 2026-10-12 · Region: us-east-1 · Account: `<ACCOUNT_ID>` · Function: `llmops-admin`

## Pre-change findings (read-only)

| Check | Result |
|---|---|
| `llmops-cost-actuals` rows for `llmops-agentic-system` | 164, **0** cost rows (all `#audit#`) — why v1 showed $0.00 |
| Cost-allocation tags active | none for llmops (`project` inactive) |
| AWS Budgets | `bedrock-monthly-dev` $1000, `agentic-usecases-monthly` $100, actual $0 (payer-billed), 0 Budget Actions |
| Deployed `llmops-admin` vs `main` | every source file byte-identical to `main` at `1ef579d` |

## Deploy

Code-only: the live zip (identical to `main`) with `lambda_function.py`, `frontend.html` and
`architecture-console.svg` replaced; `update-function-code`, then `function-updated` waiter.
IAM unchanged — `BillingRead`, `CWMetricsRead`, `SageMakerRead`, `DDBConsole` already cover
every call. Rollback artifact: the pre-change zip, kept outside the repo.

## Live verification (browser, portal `?tab=cost`, 90d window)

| Sub-tab | Observed |
|---|---|
| Overview | workload $4,287.32; MTD $148.05; training (attributed) $74.96 from 68 jobs; billed $26,302.24 (account-wide); daily stacked chart rendered |
| By task | `task-8058d7176e2e5990` estimate $164.50 → actual $49.13 (run `run-20260818T142122Z-cf294acd`); `task-cf668d3a2670fe1d` $54.44 → $1.69 |
| Governance | no budgets → Notify-only; posture lists hard-stop as "phase 2 — not armed"; AWS Budgets rows rendered |

Not verified live: a `block`-mode refusal through the portal (it needs a signed-in POST);
covered offline by `tests/test_console_cost_v2.py` (11 tests).
