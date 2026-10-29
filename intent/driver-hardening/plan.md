# Plan: cap agent-written reads (Batch 3, slice 1)

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** 426c96e94b5e6c7ea4ebe774abca67bcd52b7d0f
- **Status:** accepted

`Accepted-for` is the merge base with `main` (PR #192's merge).

## Files changed

1. `orchestration/harness_driver/handler.py`, `orchestration/conductor_tools.py`,
   `orchestration/publish_provenance/handler.py`.
2. Tests: `tests/test_driver_read_caps.py` (new), `tests/negative_controls/monitor_dispatch.py`,
   `tests/redaction_scan.py`, `tests/test_redaction_scan.py`.
3. Records: `CHANGELOG.md`, `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md`,
   `deploy/evidence/VERIFICATION_driver_hardening.md` (new), `intent/driver-hardening/{intent,spec,plan}.md`, `.sdlc/active`.

## Deploy

`deploy/07_lambdas.py` (driver + publish_provenance). No IAM or infra change. Offline-verifiable; no canary needed.

## Rollback

Revert the commit and redeploy the two Lambdas.
