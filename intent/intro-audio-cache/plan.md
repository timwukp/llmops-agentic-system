# Plan: clip URLs carry the clip's hash

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** b8b570a9eff37cc70d6c48e24e3b48f5a5f574c2
- **Status:** accepted

`Accepted-for` is the merge base with `main` (PR #187's merge).

## Files changed

1. `deploy/console/intro/build_intro.py`, `deploy/console/intro/page.template.html`,
   `deploy/console/intro/record_video.py`, `deploy/console/lambda_function.py` (comment only).
2. Tests: `tests/test_intro_bundle.py`, `tests/test_intro_player.js`,
   `tests/negative_controls/monitor_dispatch.py`, `tests/redaction_scan.py`,
   `tests/test_redaction_scan.py` (tracked-file count).
3. Records: `CHANGELOG.md`, `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md`,
   `deploy/evidence/VERIFICATION_intro_v2.md`, `intent/intro-audio-cache/{intent,spec,plan}.md`,
   `.sdlc/active`.

## Rollback

Revert the commit and redeploy the console. No audio changes.
