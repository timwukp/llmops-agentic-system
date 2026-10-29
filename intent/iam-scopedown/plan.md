# Plan: IAM scope-down (Batch 2a)

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** 56bf1d901db5166507a4e999f9e9ff2199be5f46
- **Status:** accepted

`Accepted-for` is the merge base with `main` (PR #191's merge).

## Files changed

1. `deploy/iam/harness_execution_role.json`, `deploy/console/iam-policy.json`,
   `orchestration/start_pipeline/handler.py`.
2. Tests: `tests/test_iam_scopedown.py` (new), `tests/test_finops.py`, `tests/test_orchestration.py`,
   `tests/negative_controls/monitor_dispatch.py`, `tests/redaction_scan.py`, `tests/test_redaction_scan.py`.
3. Records: `CHANGELOG.md`, `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md`,
   `deploy/evidence/VERIFICATION_iam_scopedown.md` (new), `intent/iam-scopedown/{intent,spec,plan}.md`, `.sdlc/active`.

## Deploy

`deploy/01_iam.py` (harness role) and `deploy/console/deploy.sh` (console role). Before and after,
run `simulate_principal_policy` on the harness role for: events:PutEvents (now Deny), PutObject on
contracts/reports (now Deny), CreateModelPackage with ModelApprovalStatus=Approved (Deny), and the
actions agents legitimately use (still Allow).

## Rollback

Revert the commit and re-run 01_iam.py + the console deploy.
