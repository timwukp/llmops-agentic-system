# Plan: Cost guardrails in the console

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** aefb1f3925c8299fddf6af33ecefad4504dd8931
- **Status:** accepted

`Accepted-for` is the merge base with `main` at acceptance (PR #147's merge).

## Files changed

Source:
1. `deploy/console/lambda_function.py` — `get_cost_policy`, `set_cost_policy`,
   `budget_sweep`, `_running_runs`, `_stop_run`; gate reads the policy; routes
   `POST /api/cost/policy`, `POST /api/cost/sweep`; handler entry `mode == "budget-sweep"`;
   in-progress jobs priced by elapsed time.
2. `deploy/console/frontend.html` — Enforcement policy card with two switches and *Run budget
   check now*; budget form loses its mode field.
3. `deploy/console/iam-policy.json` — `CostGuardrailStop`.
4. `deploy/console/deploy.sh` — rule `llmops-budget-sweep`, target, Lambda permission.
5. `tests/test_console_cost_v2.py` — 21 tests.

Derived claims and docs:
6. `docs/gen_architecture_svg.py` + `docs/architecture-console.svg` — 35 → 37 handlers.
7. `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md` — 1697 → 1707.
8. `tests/redaction_scan.py`, `tests/test_redaction_scan.py` — census 217 → 220.
9. `docs/COST.md`, `docs/COST.zh-TW.md`, `README.md`, `README.zh-TW.md`,
   `docs/ARCHITECTURE.md`, `docs/ARCHITECTURE.zh-TW.md`, `deploy/console/README.md`,
   `PROJECT_STATE.md`, `CHANGELOG.md`, `deploy/evidence/VERIFICATION_cost_v2.md`.
10. `.sdlc/active` — `cost-guardrails`.
