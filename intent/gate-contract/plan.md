# Plan: gate/1

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** 95182dc8dcf7d1e5972d52e210560f3c4e128d96
- **Status:** accepted

`Accepted-for` is the merge base with `main` (PR #189's merge).

## Files changed

1. `pipeline/contracts/gate.schema.json` (new), `pipeline/contracts/gate_contract.py` (new),
   `pipeline/contracts/provenance.py`, `orchestration/harness_driver/handler.py`,
   `orchestration/start_pipeline/handler.py`,
   `deploy/07_lambdas.py`, `agents/eval/harness.json`, `deploy/console/lambda_function.py` (comment).
2. Docs: `AGENTS.md`, `docs/ARCHITECTURE.md`, `docs/ARCHITECTURE.zh-TW.md`.
3. Tests: `tests/test_gate_contract.py` (new), `tests/test_orchestration.py`,
   `tests/negative_controls/monitor_dispatch.py`, `tests/redaction_scan.py`,
   `tests/test_redaction_scan.py` (tracked-file count).
4. Records: `CHANGELOG.md`, `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md`,
   `deploy/evidence/VERIFICATION_gate_contract.md` (new), `deploy/evidence/VERIFICATION_intro_v2.md`
   (the owner's playback of the captions fix), `intent/gate-contract/{intent,spec,plan}.md`,
   `.sdlc/active`.

## Deploy

The driver Lambda (`deploy/07_lambdas.py`) and the eval harness prompt (`deploy/05_harnesses.py`).
The driver change is backward compatible with the old prompt only in the sense that it will reject
a gate.json in an old shape, so both deploy together, the prompt first.

## Rollback

Revert the commit and redeploy both.
