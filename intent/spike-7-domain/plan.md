# Plan: S7 domain part

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** 9d28eccbde36ed595d464334ef00eff479781485
- **Status:** accepted

`Accepted-for` is the merge base with `main` at acceptance (PR #159's merge).

## Files changed

1. `tools/spikes/s7_lakehouse_substrate.py` — the domain part.
2. `tests/test_spikes.py` — domain-part tests. `tests/test_runner.py` — exemptions, literal-client guard.
3. `deploy/evidence/SPIKE_07_s7_lakehouse_substrate.md` — substrate + domain part.
4. `tools/spikes/README.md`, `CHANGELOG.md`.
5. `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md` — counts. `tests/redaction_scan.py`,
   `tests/test_redaction_scan.py` — census counts.
6. `intent/spike-7-domain/{intent,spec,plan}.md`, `.sdlc/active`.
