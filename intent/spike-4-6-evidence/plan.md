# Plan: S4 + S6 evidence PR

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** 6d5fe315f3fa3bf9fcda0ab7226e068124227242
- **Status:** accepted

`Accepted-for` is the merge base with `main` at acceptance (PR #152's merge).

## Files changed

1. `tools/spikes/s4_pii_recall_zhtw.py`, `tools/spikes/s6_vpc_harness.py` — run() implemented.
2. `tools/spikes/_spike.py` — LEFT RUNNING reporting, `cleanup` action.
3. `deploy/evidence/SPIKE_04_s4_pii_recall_zhtw.md`, `deploy/evidence/SPIKE_06_s6_vpc_harness.md` — new.
4. `tests/test_spikes.py` — 54 tests.
5. `tools/spikes/README.md`, `CHANGELOG.md`, `PROJECT_STATE.md`.
6. `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md` — counts. `tests/redaction_scan.py`,
   `tests/test_redaction_scan.py` — counts, plus a narrow precision fix: a 12-digit run inside a complete
   40/64-hex digest (this plan's Accepted-for SHA) is not an account id; six tests pin both directions.
7. `intent/spike-4-6-evidence/{intent,spec,plan}.md`, `.sdlc/active`.
