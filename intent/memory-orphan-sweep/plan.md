# Plan: memory orphans in the sweep

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** 7349dc60ba31ab985d13d2580eeb4de809b874a1
- **Status:** accepted

`Accepted-for` is the merge base with `main` at acceptance (PR #162's merge).

## Files changed

1. `agents/monitor/harness.json` — sweep clause.
2. `deploy/iam/harness_execution_role.json` — two read statements.
3. `tests/test_orchestration.py` — `_CLI_TO_IAM` entries.
4. `tests/test_harness_memory.py` — sweep/IAM guards.
5. `AGENTS.md`, `CHANGELOG.md`, `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md`.
6. `tests/redaction_scan.py`, `tests/test_redaction_scan.py` — counts, if they move.
7. `intent/memory-orphan-sweep/{intent,spec,plan}.md`, `.sdlc/active`.
