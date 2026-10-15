# Plan: Gold export (Phase 1 PR-08)

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** 8f7c618d4e3b6863cb2ffa2365f55f9f05caccec
- **Status:** accepted

`Accepted-for` is the merge base with `main`: PR #164's merge. The plan was accepted on #162's merge
and re-bound twice, when #163 and then #164 landed first. Neither touched this plan's source files
beyond `deploy/console/lambda_function.py` (#163), which merged without conflict; the conflicts were the
census, the documented test counts and the changelog.

## Files changed

1. `pipeline/lakehouse/export.py`, `pipeline/lakehouse/glue/export_gold.py` — new.
2. `deploy/10_lakehouse.py` — export job, grants, `--verify` cases, smoke; S3 labels without the bucket;
   the workgroup sets no result encryption (it breaks Athena writes to CMK tables), reconciled and verified.
3. `orchestration/harness_driver/handler.py` — `_verify_gold_extract`; several attestations per stage.
4. `pipeline/contracts/manifest.schema.json` — gold_extract fields.
5. `deploy/03_storage.py` — Macie coverage by prefix; extracts; each job's scope read from
   `describe_classification_job` (list items carry no scoping).
6. `deploy/console/lambda_function.py`, `deploy/console/frontend.html` — the export on the Data tab.
7. `tests/test_lakehouse_export.py` — new. `tests/test_manifest_schema.py`, `tests/test_lakehouse_storage.py`,
   `tests/test_console_data.py` — extended. `tests/negative_controls/monitor_dispatch.py` — m53/m56
   re-anchored on `ensure_pii_scan`'s `prefix` parameter. `tests/test_console_tasks.py` — the Macie double
   returns the production list shape; coverage-from-describe tests.
8. `deploy/evidence/VERIFICATION_gold_export.md`, `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md`.
9. `AGENTS.md`, `PROJECT_STATE.md`, `CHANGELOG.md`, `tests/redaction_scan.py`, `tests/test_redaction_scan.py`.
10. `intent/gold-export/{intent,spec,plan}.md`, `.sdlc/active`.
