# Plan: CI / supply-chain hardening (Batch 4)

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** 5c688eaba81cec02ab37ba8e3e1d629f677da56d
- **Status:** accepted

`Accepted-for` is the merge base with `main` (PR #193's merge).

## Files changed

1. `tests/redaction_scan.py`, `hooks/pre-commit`, `tools/push_via_api.py`,
   `.github/workflows/run-pipeline.yml`, `.github/workflows/ci.yml`, `.github/workflows/redaction-check.yml`.
2. Tests: `tests/test_ci_hardening.py` (new), `tests/negative_controls/monitor_dispatch.py`,
   `tests/test_redaction_scan.py`.
3. Records: `CHANGELOG.md`, `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md`,
   `deploy/evidence/VERIFICATION_ci_hardening.md` (new), `intent/ci-hardening/{intent,spec,plan}.md`, `.sdlc/active`.

## Deploy

None — CI config and local tooling only.

## Rollback

Revert the commit.
