# Plan: held-out acceptance set and Phase 1 live evidence (PR-11)

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** 80a13c9d340a8f0279aa284680530b319724ac56
- **Status:** accepted

`Accepted-for` is the merge base with `main` at acceptance (PR #172's merge; accepted on #170's and re-bound when #171 and #172 landed first).

## Files changed

1. `pipeline/lakehouse/generate.py`: paraphrase families, the acceptance set, version 2.
2. `pipeline/lakehouse/export.py`, `pipeline/lakehouse/glue/export_gold.py`: the acceptance set in the bundle.
3. `deploy/10_lakehouse.py`: `--acceptance_uri` in `run_arguments`.
3a. After the owner ruled the borderline format gate a failure: `pipeline/lakehouse/transform.py` (Gold promotes
    no pair carrying a redaction token; `GOLD_DQDL` asserts it), `pipeline/lakehouse/glue/silver_to_gold.py`
    (`excluded_redacted` in the report), `tests/test_lakehouse_transform.py`.
4. `orchestration/state_machine.asl.json`: the same argument on `LakehouseExport`; `FinetuneAnalyze` 3600 → 86400 s
   (found live: the full job's capacity wait outlasted analyze's ceiling). `tests/test_orchestration.py`: its class.
5. `orchestration/harness_driver/handler.py`, `pipeline/contracts/manifest.schema.json`: the attestation; and
   `_deliver_if_already_terminal` with its grant in `deploy/iam/lambda_roles.json` (a defect found live: a job that
   finished before its token was parked was never resumed).
6. `agents/data-prep/harness.json`, `agents/eval/harness.json`: the review and fallback clauses.
7. Tests: `tests/test_lakehouse_generate.py`, `tests/test_lakehouse_export.py`, `tests/test_manifest_schema.py`,
   `tests/test_lakehouse_mode.py`; `tests/negative_controls/monitor_dispatch.py` (m354, m355).
8. Docs: `deploy/evidence/VERIFICATION_lakehouse_phase1.md`, `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md`,
   `PROJECT_STATE.md`, `CHANGELOG.md`, `tests/redaction_scan.py`, `tests/test_redaction_scan.py`.
9. `intent/lakehouse-phase1-evidence/{intent,spec,plan}.md`, `.sdlc/active`.
