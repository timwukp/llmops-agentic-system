# Plan: memory orphans in the sweep

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** adc30b1ce979795c01e0b4d87a5054cef968f141
- **Status:** accepted

`Accepted-for` is the merge base with `main` (PR #163's merge). It was first written against #162's,
then re-bound when #163 (console S3 scope) landed and `main` was merged in to resolve count conflicts;
#163 touches none of this plan's files except the shared counts, which the merge sums.

## Files changed

1. `agents/monitor/harness.json` — sweep clause.
2. `deploy/iam/harness_execution_role.json` — two read statements.
3. `tests/test_orchestration.py` — `_CLI_TO_IAM` entries.
4. `tests/test_harness_memory.py` — sweep/IAM guards.
5. `AGENTS.md`, `CHANGELOG.md`, `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md`.
6. `tests/redaction_scan.py`, `tests/test_redaction_scan.py` — counts, if they move.
7. `intent/memory-orphan-sweep/{intent,spec,plan}.md`, `.sdlc/active`.
