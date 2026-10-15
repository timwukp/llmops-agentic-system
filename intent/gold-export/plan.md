# Plan: Gold export (Phase 1 PR-08)

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** adc30b1ce979795c01e0b4d87a5054cef968f141
- **Status:** accepted

`Accepted-for` is the merge base with `main`: PR #163's merge. The plan was accepted on #162's merge
and re-bound when #163 landed first; #163 touched none of this plan's source files except
`deploy/console/lambda_function.py`, which merged without conflict.

## Files changed

1. `pipeline/lakehouse/export.py`, `pipeline/lakehouse/glue/export_gold.py` — new.
2. `deploy/10_lakehouse.py` — export job, grants, `--verify` cases, smoke; S3 labels without the bucket;
   the workgroup sets no result encryption (it breaks Athena writes to CMK tables), reconciled and verified.
3. `orchestration/harness_driver/handler.py` — `_verify_gold_extract`; several attestations per stage.
4. `pipeline/contracts/manifest.schema.json` — gold_extract fields.
5. `deploy/03_storage.py` — Macie coverage by prefix; extracts.
6. `deploy/console/lambda_function.py`, `deploy/console/frontend.html` — the export on the Data tab.
7. `tests/test_lakehouse_export.py` — new. `tests/test_manifest_schema.py`, `tests/test_lakehouse_storage.py`,
   `tests/test_console_data.py` — extended. `tests/negative_controls/monitor_dispatch.py` — m53/m56
   re-anchored on `ensure_pii_scan`'s `prefix` parameter.
8. `deploy/evidence/VERIFICATION_gold_export.md`, `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md`.
9. `AGENTS.md`, `PROJECT_STATE.md`, `CHANGELOG.md`, `tests/redaction_scan.py`, `tests/test_redaction_scan.py`.
10. `intent/gold-export/{intent,spec,plan}.md`, `.sdlc/active`.
