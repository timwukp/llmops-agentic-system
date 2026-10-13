# Plan: manifest schema truth (Phase 1 PR-01)

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** 3a3a61d7a2b0961f39e496a7f92631c91dc26338
- **Status:** accepted

`Accepted-for` is the merge base with `main` at acceptance (PR #153's merge).

## Files changed

1. `pipeline/contracts/manifest.schema.json` — corrected to the writers.
2. `tests/test_manifest_schema.py` — new, 8 tests.
3. `orchestration/harness_driver/handler.py` — one comment that cited `models.student.endpoint_name`.
4. `deploy/evidence/VERIFICATION_manifest_schema.md` — live survey, old vs new schema.
5. `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md` — evidence row, counts.
6. `tests/redaction_scan.py`, `tests/test_redaction_scan.py` — census counts.
7. `CHANGELOG.md`.
8. `intent/manifest-schema-truth/{intent,spec,plan}.md`, `.sdlc/active`.
