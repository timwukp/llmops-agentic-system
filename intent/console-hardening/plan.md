# Plan: console hardening (Batch 1)

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** e8bc69965cfc966a958d6fc3c9326e40991b3628
- **Status:** accepted

`Accepted-for` is the merge base with `main` (PR #190's merge).

## Files changed

1. `deploy/console/lambda_function.py` (projectors, approver guard, budget/cost/500 hardening),
   `deploy/console/frontend.html` (two inline handlers).
2. Tests: `tests/test_console_public_scope.py` (new), `tests/negative_controls/monitor_dispatch.py`
   (6 new controls + re-anchor m257), `tests/redaction_scan.py`, `tests/test_redaction_scan.py`.
3. Records: `CHANGELOG.md`, `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md`,
   `deploy/evidence/VERIFICATION_console_hardening.md` (new),
   `intent/console-hardening/{intent,spec,plan}.md`, `.sdlc/active`.

## Deploy

The console Lambda only (`deploy/console/deploy.sh`). No IAM change in this batch.

## Rollback

Revert the commit and redeploy the console.
