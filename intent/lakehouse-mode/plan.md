# Plan: lakehouse pipeline modes (Phase 1 PR-09)

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** 321d4728d53cf155e0327e9d74e521c18ed24bdb
- **Status:** accepted

`Accepted-for` is the merge base with `main` at acceptance (PR #166's merge).

## Files changed

1. `orchestration/state_machine.asl.json`: two mode branches, five Glue states, `LakehouseReview`;
   the Pass states carry `lakehouse`.
2. `orchestration/start_pipeline/handler.py`: the `lakehouse` object in the input and the manifest, mode tables,
   refusals. `pipeline/contracts/manifest.schema.json`: `lakehouse`.
3. `orchestration/harness_driver/handler.py`: `STAGE_EVENT_MAP` for `lakehouse_review`.
4. `pipeline/lakehouse/glue/generate_landing.py`: new, the generator job.
5. `deploy/10_lakehouse.py`: the generate job and its role, DefaultArguments, smoke through the job,
   `--verify` cases for the generator role.
6. `deploy/iam/sfn_execution_role.json`, `deploy/iam/harness_execution_role.json`,
   `deploy/iam/sagemaker_execution_role.json`: the grants.
7. `agents/data-prep/harness.json`, `agents/finetune/harness.json`, `agents/eval/harness.json`: prompts.
8. `deploy/console/lambda_function.py`: the new states in the pipeline view.
9. Tests: `tests/test_lakehouse_mode.py` (new), `tests/test_orchestration.py`, `tests/test_finops.py`,
   `tests/test_lakehouse_storage.py`; `tests/negative_controls/monitor_dispatch.py` (m189 re-anchored, m346-m348);
   `tests/test_cost_model.py` (a pre-existing uncaught control: the COST.md guard's window is now the passage).
10. Docs: `deploy/evidence/VERIFICATION_lakehouse_mode.md`, `docs/TEST_RESULTS.md`,
    `docs/TEST_RESULTS.zh-TW.md`, `docs/ARCHITECTURE.md`, `docs/ARCHITECTURE.zh-TW.md`, `agents/README.md`,
    `PROJECT_STATE.md`, `CHANGELOG.md`, `tests/redaction_scan.py`, `tests/test_redaction_scan.py`.
11. `intent/lakehouse-mode/{intent,spec,plan}.md`, `.sdlc/active`.
