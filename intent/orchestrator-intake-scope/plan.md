# Plan: orchestrator intake scope

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** 321d4728d53cf155e0327e9d74e521c18ed24bdb
- **Status:** accepted

`Accepted-for` is the merge base with `main` (PR #166's merge). It was first written against #164's, then
re-bound when #165 (Gold export) and then #166 (memory-orphan sweep evidence) landed and `main` was
merged in. Neither touches this change's source files.

## Files changed

1. `agents/orchestrator/harness.json`: the two sentences.
2. `tests/test_console_s3_scope.py`: three prompt guards.
   `tools/probe_orchestrator_intake.py` (new): tests a prompt before it is deployed, via InvokeHarness's
   per-call `systemPrompt`, against the live prompt as baseline.
3. `deploy/evidence/VERIFICATION_orchestrator_intake_scope.md` (new), `docs/TEST_RESULTS.md`,
   `docs/TEST_RESULTS.zh-TW.md`, `CHANGELOG.md`, `PROJECT_STATE.md`.
4. `tests/redaction_scan.py`, `tests/test_redaction_scan.py`: census counts.
5. `intent/orchestrator-intake-scope/{intent,spec,plan}.md`, `.sdlc/active`.
