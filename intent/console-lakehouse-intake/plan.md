# Plan: console intake for the lakehouse modes

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** 5766d04f5e7614a61f0d52a947a727b2b0a97a50
- **Status:** accepted

`Accepted-for` is the merge base with `main` at acceptance (PR #173's merge).

## Files changed

1. `deploy/console/lambda_function.py`: registry entries, intake aliases, `_verify_lakehouse_dataset`.
2. `agents/orchestrator/harness.json`: the two modes in the menu.
3. Tests: `tests/test_console_lakehouse_intake.py` (new), `tests/test_console_tasks.py`; `tests/negative_controls/monitor_dispatch.py` (m356).
4. Docs: `deploy/evidence/VERIFICATION_console_lakehouse_intake.md`, `docs/TEST_RESULTS.md`,
   `docs/TEST_RESULTS.zh-TW.md`, `CHANGELOG.md`, `tests/redaction_scan.py`,
   `tests/test_redaction_scan.py`.
5. `intent/console-lakehouse-intake/{intent,spec,plan}.md`, `.sdlc/active`.
