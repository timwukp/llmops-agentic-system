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

## Cost guardrails: visible switches + running-job alarm (2026-10-12)

| Step | Result |
|---|---|
| Live IAM before change | `LlmopsAdminPerms` byte-equal (sorted JSON) to `deploy/console/iam-policy.json`, so applying the file added exactly one statement: `CostGuardrailStop` |
| IAM applied | `put-role-policy` OK; delta = `states:StopExecution` on `llmops-pipeline` executions, `sagemaker:StopTrainingJob` on `llmops-*` / `arc2v2-*` |
| Code | `update-function-code` OK (zip = previous live zip with `lambda_function.py`, `frontend.html`, `architecture-console.svg` replaced; `cost_model.py`, `conductor_tools.py` verified equal to `main`) |
| Schedule | rule `llmops-budget-sweep` ENABLED `rate(15 minutes)`, target Input `{"mode":"budget-sweep"}`, Lambda permission `budget-sweep` |
| Sweep invoked with the schedule's payload | `200`, `{"trigger": "schedule", "budgets": 0, "alerts": 0, "stopped": [], "policy": {"block_launches": false, "stop_running": false}, "running_runs": 0}` |
| Portal, Governance | both switches render `aria-checked="false"` / OFF; Enforcement mode KPI "Alarm only"; posture rows OFF / OFF |
| `POST /api/cost/policy` without a token | `401`; policy read back unchanged (both false) |
| SNS | `LLMOPS_SNS_TOPIC` set on the function; topic has 1 confirmed subscriber |

Not exercised live: flipping a switch (needs an approver sign-in) and a real stop (no run was
in flight and no budget exists). Both are covered offline in `tests/test_console_cost_v2.py`
(21 tests: approver-only flip, flip logged, gate under each switch, once-per-threshold alarm,
stop OFF never stops, stop ON stops the execution and only in-progress jobs, per-task stop
targets only the overspent run, schedule payload reaches the sweep, IAM scope).
