# Plan: sweep contract

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** ecc2e50b6006c0237d3308e178cdd5b0aa62f45a
- **Status:** accepted

`Accepted-for` is the merge base with `main` (PR #176's merge). It was first written against #174's
(`94c5e5a`) and re-bound after #175 (probe/full handoff) and #176 (deploy settle-and-teardown) landed,
both touching `orchestration/harness_driver/handler.py`. The three driver conflicts were resolved by
content (`resolve_driver.py`), and the counts were re-derived on top of both.

## Files changed

1. `pipeline/contracts/sweep.schema.json`, `pipeline/contracts/sweep_contract.py`,
   `pipeline/contracts/sweep.example.json` (conforming example; the shared test fixture) — new.
2. `orchestration/harness_driver/handler.py` — the check and the rejection message.
3. `deploy/07_lambdas.py` — `VENDOR_DATA`.
4. `agents/monitor/harness.json` — sweep clause.
5. `tests/test_sweep_contract.py` — new; `tests/test_orchestration.py` — sweep fixtures carry a
   conforming body (`sweep_s3`), plus an end-to-end reject-then-rewrite test.
6. `CHANGELOG.md`, `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md`.
7. `tests/redaction_scan.py`, `tests/test_redaction_scan.py` — counts.
8. `intent/sweep-contract/{intent,spec,plan}.md`, `.sdlc/active`.
