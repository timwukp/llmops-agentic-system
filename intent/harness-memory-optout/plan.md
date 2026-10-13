# Plan: explicit create-time memory

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** 190babc064916f62ff6cd95f022a7facb8955cd2
- **Status:** accepted

`Accepted-for` is the merge base with `main` at acceptance (PR #154's merge).

## Files changed

1. `agents/data-prep/harness.json`, `agents/finetune/harness.json`, `agents/eval/harness.json`,
   `agents/deploy/harness.json`, `agents/monitor/harness.json`, `agents/orchestrator/harness.json`,
   `agents/finops/harness.json` — `"memory": {"disabled": {}}` + corrected `_memory_note`.
2. `deploy/create_harness.py` — `create_time_memory()`, called in `main()`.
3. `deploy/05_harnesses.py` — calls it on create; `managed_memory_leak()` read-back on create.
4. `deploy/validate_config.py` — memory union validation.
5. `tools/spikes/s6_vpc_harness.py` — explicit opt-out.
6. `tests/test_harness_memory.py` — new guards and behaviour tests.
7. `AGENTS.md`, `agents/README.md`, `agents/harness.json.template`, `deploy/iam/execution_role_base.json` — corrected claims.
8. `deploy/evidence/VERIFICATION_harness_memory_audit.md` — the read-only audit.
9. `CHANGELOG.md`, `PROJECT_STATE.md`, `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md`.
   `tests/redaction_scan.py`, `tests/test_redaction_scan.py` — tracked-file count 255 → 260 and
   12-digit runs 76 → 77 (the suite's dummy account, named once in the new test).
10. `intent/harness-memory-optout/{intent,spec,plan}.md`, `.sdlc/active`.
