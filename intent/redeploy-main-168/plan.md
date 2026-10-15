# Plan: redeploy record

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** ca941fe2d4a18e1d29e330d33bbccae115288181
- **Status:** accepted

`Accepted-for` is the merge base with `main` (PR #171's merge). It was first written against #170's;
#171 (orchestrator intake scope) landed while this PR was open and touches none of this plan's files
except CHANGELOG and the shared counts, which were re-derived on top of it.

## Files changed

1. `deploy/evidence/VERIFICATION_sweep_unheard_calls.md` — §7.
2. `PROJECT_STATE.md`, `AGENTS.md`, `CHANGELOG.md`.
3. `tests/redaction_scan.py`, `tests/test_redaction_scan.py` — tracked-file count 336 → 339.
4. `intent/redeploy-main-168/{intent,spec,plan}.md`, `.sdlc/active`.
