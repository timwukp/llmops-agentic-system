# Plan: Introduction v2

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** 6a08d9ebe5dbcca1fdfef93448eb73b1640e5c22
- **Status:** accepted

`Accepted-for` is the merge base with `main` (PR #185's merge).

## Files changed

1. `deploy/console/intro/narration.json`, `deploy/console/intro/page.template.html`,
   `deploy/console/intro/durations.json`, `deploy/console/intro/audio/**` (35 clips and the stamps).
2. `deploy/console/frontend.html`, `deploy/console/README.md`, `README.md`, `README.zh-TW.md`.
3. Tests: `tests/test_intro_narration.py` (new), `tests/test_intro_bundle.py`,
   `tests/negative_controls/monitor_dispatch.py`, `tests/redaction_scan.py`,
   `tests/test_redaction_scan.py` (tracked-file count).
4. Records: `CHANGELOG.md`, `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md`,
   `deploy/evidence/VERIFICATION_intro_v2.md` (new), `intent/intro-v2/{intent,spec,plan}.md`,
   `.sdlc/active`.

## Rollback

Revert the commit (the v1 clips are in git history) and redeploy the console.
