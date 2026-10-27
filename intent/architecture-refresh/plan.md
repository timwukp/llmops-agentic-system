# Plan: the Architecture page and docs, refreshed

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** cfeade331d8743a20daf7465a1e7d9ac285f6cfe
- **Status:** accepted

`Accepted-for` is the merge base with `main` (PR #184's merge).

## Files changed

1. `docs/gen_architecture_svg.py`, `docs/architecture-high-level.svg` (regenerated).
2. `deploy/console/frontend.html`.
3. `docs/ARCHITECTURE.md`, `docs/ARCHITECTURE.zh-TW.md`.
4. Tests: `tests/test_architecture_page.py` (new), `tests/negative_controls/monitor_dispatch.py`,
   `tests/redaction_scan.py`, `tests/test_redaction_scan.py` (tracked-file count).
5. Records: `CHANGELOG.md`, `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md`,
   `deploy/evidence/VERIFICATION_architecture_refresh.md` (new),
   `intent/architecture-refresh/{intent,spec,plan}.md`, `.sdlc/active`.

## Rollback

Revert the commit, regenerate, redeploy the console.
