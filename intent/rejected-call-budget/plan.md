# Plan: refused-call budget

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** e51a56a09359998af38bc2e76f0e52398cc8c5c3
- **Status:** accepted

Pre-resolved overnight on 94c5e5a + a40836 + 945982 + a96115 (ORDER v1 steps 1-3). It was rebuilt on
`main` after #175, #176, #177 and #178 landed, by the coordinating session (a40836), because this
change's own session (1df6c6) had ended. `Accepted-for` is #178's merge.

## Files changed

1. `orchestration/harness_driver/handler.py` — constants, `_answer`, the loop-top check, the
   continuation key.
2. `tests/test_orchestration.py` — four tests; `tests/negative_controls/monitor_dispatch.py` — three
   controls.
3. `AGENTS.md`, `CHANGELOG.md`, `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md` — counts.
4. `tests/redaction_scan.py`, `tests/test_redaction_scan.py` — census.
5. `intent/rejected-call-budget/{intent,spec,plan}.md`, `.sdlc/active`.
