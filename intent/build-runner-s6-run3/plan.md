# Plan: build runner + S6 run 3

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** 190babc064916f62ff6cd95f022a7facb8955cd2
- **Status:** accepted

`Accepted-for` is the merge base with `main` at acceptance (PR #154's merge).

## Files changed

1. `tools/runner/runner.py`, `tools/runner/README.md` — new.
2. `tests/test_runner.py` — new.
3. `tools/spikes/_spike.py` — `boundary_kwargs`, cleanup output.
4. `tools/spikes/s4_pii_recall_zhtw.py`, `tools/spikes/s7_lakehouse_substrate.py` — boundary on created roles.
5. `tools/spikes/s6_vpc_harness.py` — run 3 diagnostics and variants.
6. `tests/test_spikes.py` — S6 run 3 tests.
7. `deploy/evidence/SPIKE_06_s6_vpc_harness.md` — run 3 evidence.
8. `tools/spikes/README.md`, `PROJECT_STATE.md`, `CHANGELOG.md`.
9. `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md` — counts. `tests/redaction_scan.py`,
   `tests/test_redaction_scan.py` — census counts.
10. `intent/build-runner-s6-run3/{intent,spec,plan}.md`, `.sdlc/active`.
