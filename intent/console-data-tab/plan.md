# Plan: console Data tab

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** d8b989456dc0730a6542fc7a44525eda4f3ff885
- **Status:** accepted

`Accepted-for` is the merge base with `main` at acceptance (PR #161's merge).

## Files changed

1. `deploy/console/lambda_function.py`, `deploy/console/frontend.html`, `deploy/console/iam-policy.json`,
   `deploy/console/deploy.sh` (`$TMPDIR`), `deploy/console/README.md`.
2. `pipeline/lakehouse/glue/land_bronze.py`, `pipeline/lakehouse/glue/bronze_to_silver.py`,
   `pipeline/lakehouse/glue/silver_to_gold.py` — report rows/snapshots/plant check.
3. `deploy/10_lakehouse.py` — smoke passes the land report and the plants.
4. `tests/test_console_data.py` — new. `tests/test_lakehouse_storage.py` — report contract.
5. `docs/gen_architecture_svg.py`, `docs/architecture-console.svg` (regenerated), `docs/ARCHITECTURE.md`,
   `docs/ARCHITECTURE.zh-TW.md` — route and tab counts.
6. `deploy/evidence/VERIFICATION_console_data_tab.md`, `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md`.
7. `CHANGELOG.md`, `tests/redaction_scan.py`, `tests/test_redaction_scan.py`.
8. `intent/console-data-tab/{intent,spec,plan}.md`, `.sdlc/active`.
