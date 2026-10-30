# Plan: process isolation + storage hardening (Batch 5)

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** 5c688eaba81cec02ab37ba8e3e1d629f677da56d
- **Status:** accepted

`Accepted-for` is the commit this branch is built on — current `main` after PR #193's merge.

## Files changed

1. `pipeline/v2/verify_sandbox.py`, `deploy/03_storage.py`.
2. Tests: `tests/test_audit_hardening.py` (new), `tests/negative_controls/monitor_dispatch.py`.
3. Records: `CHANGELOG.md`, `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md`,
   `deploy/evidence/VERIFICATION_audit_hardening.md` (new),
   `intent/audit-hardening/{intent,spec,plan}.md`, `.sdlc/active`.

## Deploy

`deploy/03_storage.py` (bucket policy + table protections; idempotent). The sandbox change ships
with whatever bundles `pipeline/v2`. No IAM or infra change. Offline-verifiable; no canary needed.

## Rollback

Revert the commit and re-run `deploy/03_storage.py`. The bucket policy and table protections are
additive; removing deletion protection to roll back a table requires an explicit
`update_table(DeletionProtectionEnabled=False)` first.
