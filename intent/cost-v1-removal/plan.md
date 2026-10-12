# Plan: remove v1 cost

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** dfd72ba63c90fb81e819a2fd63578c607d9c6fb1
- **Status:** accepted

`Accepted-for` is the merge base with `main` at acceptance (PR #148's merge).

## Files changed

Source:
1. `deploy/console/lambda_function.py` — v1 functions, routes, `BUDGET_MODE` blocking path,
   `start_run` estimate gate removed; rate-card health added to `/api/cost/v2` detail.
2. `deploy/console/frontend.html` — v1 cards and JS removed; rate-card health on Detail; Tasks
   texts no longer point at the queue.
3. `deploy/console/iam-policy.json` — `DDBCostEstimates` narrowed.
4. `deploy/console/deploy.sh` — no `BUDGET_MODE`.
5. `tests/test_console_cost.py`, `tests/test_console_tasks.py`,
   `tests/negative_controls/monitor_dispatch.py` — removals, replacements, m216 re-pointed;
   controls m80, m100–m103, m178, m178b, m266, m298, m162, m163 retired.
   `tests/test_orchestration.py` — the two tests that read the removed estimator form's key
   lists deleted (the cost_model ↔ dispatcher agreement stays covered by
   `test_every_plan_field_the_estimator_prices_from_is_one_the_dispatcher_obeys`).

Derived claims and docs:
6. `docs/gen_architecture_svg.py` + `docs/architecture-console.svg` — 37 → 32 handlers.
7. `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md` — suite and negative-control counts.
8. `tests/redaction_scan.py`, `tests/test_redaction_scan.py` — census 220 → 223.
9. `docs/COST.md`, `docs/COST.zh-TW.md`, `docs/ARCHITECTURE.md`, `docs/ARCHITECTURE.zh-TW.md`,
   `README.md`, `README.zh-TW.md`, `deploy/console/README.md`, `PROJECT_STATE.md`,
   `RECONNECT.md`, `CHANGELOG.md`, `deploy/evidence/VERIFICATION_cost_v2.md`.
10. `.sdlc/active` — `cost-v1-removal`.
