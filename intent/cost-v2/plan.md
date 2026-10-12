# Plan: Cost v2 in the console Lambda and page

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** 1ef579d01a59fc2c80cf0b22c6a70a7473562dc3
- **Status:** accepted

`Accepted-for` is the merge base with `main` at acceptance (PR #146's merge).

## Files changed

Source:
1. `deploy/console/lambda_function.py` — `cost_v2`, `cost_budgets`, `save_budget`,
   `budget_gate`, `_gate_refusal`; routes `GET /api/cost/v2`, `GET/POST /api/cost/budgets`;
   the gate wired into the start-run and task-accept routes.
2. `deploy/console/frontend.html` — six Cost sub-tabs, stacked daily chart, budget form;
   v1 panels tagged `data-csub="governance"`.
3. `tests/test_console_cost_v2.py` — 11 offline tests of the gate and budget state.

Derived claims and docs:
4. `docs/gen_architecture_svg.py` + regenerated `docs/architecture-console.svg` — 32 → 35
   route handlers.
5. `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md` — test count 1686 → 1697.
6. `tests/redaction_scan.py`, `tests/test_redaction_scan.py` — tracked-file census 212 → 217
   (two Cost v2 files, this three-file chain).
7. `docs/COST.md`, `docs/COST.zh-TW.md` §8, `deploy/console/README.md`, `CHANGELOG.md`,
   `PROJECT_STATE.md`, `deploy/evidence/VERIFICATION_cost_v2.md`.
8. `.sdlc/active` — `cost-v2`.

## Verification

Full offline suite; live portal check of every sub-tab (recorded in the evidence file);
rollback = the pre-change `llmops-admin` zip via `update-function-code`.
