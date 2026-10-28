# Spec: console hardening (Batch 1)

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. **Public-read projection** (`deploy/console/lambda_function.py`): `overview()` returns each run row
   through `_public_run` (allowlist `PUBLIC_RUN_FIELDS` = the six fields the page renders);
   `run_detail()` keeps the full manifest local for the gate/OOD computation but returns it through
   `_public_manifest` (per-stage `status`/`metrics`/`evidence` only). Drops `task_token`,
   `driver_beat_payload`, `signed_gates`, `approval.*`, `params`, `plan`, `models`.
2. **Approver-only routes:** a central guard denies a non-approver on `APPROVER_ONLY_POSTS`
   (start-run, cost/budgets, cost/sweep, finops-run, batch-eval, insights-report, native-rec[-apply],
   optimize[-apply]); `/api/tasks/<id>/accept` requires the approver group. Self-approval is NOT yet
   banned — one admin account both creates and approves; separation of duties waits for per-person
   accounts (the kept-Cognito decision).
3. **Budget values:** `save_budget` rejects non-finite limits (`math.isfinite`) and limits over
   `BUDGET_LIMIT_MAX`; thresholds that are not integers are a 400, not a 500.
4. **Cost windows:** `cost_v2` snaps `days` to `COST_DAYS_CHOICES` (7/30/90), capping distinct cache keys.
5. **Errors:** the catch-all returns `{error: "internal error", request_id}` and logs the detail.
6. **Frontend:** two `'${esc(x)}'` inline handlers use `jstr(x)` like the rest of the file.
7. **Guards:** `tests/test_console_public_scope.py` derives the run-row allowlist from `renderRuns`
   and checks the run-detail consumer reads the manifest only via `.stages`; 6 negative controls.

Deferred to a focused follow-up: a server-side cost estimate for start-run (now approver-only, so the
group-less exploit is already closed); requiring auth on the cost GET routes.
