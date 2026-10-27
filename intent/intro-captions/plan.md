# Plan: caption pages that break between sentences

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** 08e2cdc16baf7d43818b302110bd2c21a8315224
- **Status:** accepted

`Accepted-for` is the merge base with `main` (PR #188's merge).

## Files changed

1. `deploy/console/intro/build_intro.py`, `deploy/console/intro/page.template.html`,
   `deploy/console/intro/narration.json` (`caption_breaks` only; no spoken text changes, so no clip
   is re-synthesized).
2. Tests: `tests/test_intro_captions.py` (new), `tests/test_intro_player.js`,
   `tests/negative_controls/monitor_dispatch.py`, `tests/redaction_scan.py`,
   `tests/test_redaction_scan.py` (tracked-file count).
3. Records: `CHANGELOG.md`, `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md`,
   `deploy/evidence/VERIFICATION_intro_v2.md`, `intent/intro-captions/{intent,spec,plan}.md`,
   `.sdlc/active`.

## Rollback

Revert the commit and redeploy the console.
