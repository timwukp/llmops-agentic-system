# Plan: hearing the call, seeing the failure

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** 321d4728d53cf155e0327e9d74e521c18ed24bdb
- **Status:** accepted

`Accepted-for` is the merge base with `main` (PR #166's merge). It was first written against #165's;
#166 (the memory-orphan sweep's live evidence) landed during this change and touches none of this
plan's files except the shared counts, which were re-derived on top of it.

## Files changed

1. `orchestration/harness_driver/handler.py` — `_drain`, `_tool_result_content`,
   `parse_typed_call`, the turn loop's two recoveries, `PROTOCOL_COUNTERS`, and
   `_settle_scheduled_outcome` called from `handler`.
2. `orchestration/monitor_sweep/handler.py` — `OUTCOME_SK`, `outcome_sk` in the payload, docstring.
3. `agents/monitor/harness.json` — one TURN-END INVARIANT sentence.
4. `tests/test_orchestration.py` — replays of the four live failures plus the outcome-row tests.
   `tests/negative_controls/monitor_dispatch.py` — m203/m207 re-anchored on the widened gate (the
   condition is matched, not spelled), and m346–m350 for the new guards.
5. `AGENTS.md`, `CHANGELOG.md`, `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md`,
   `docs/ARCHITECTURE.md`, `docs/ARCHITECTURE.zh-TW.md` (the new protocol counter),
   `deploy/evidence/VERIFICATION_sweep_unheard_calls.md`.
6. `tests/redaction_scan.py`, `tests/test_redaction_scan.py` — tracked-file count 318 → 322.
7. `intent/sweep-unheard-calls/{intent,spec,plan}.md`, `.sdlc/active`.

## Not in this change

- Deploying. `07_lambdas.py` (driver, monitor-sweep) and `05_harnesses.py` (monitor prompt) wait for
  the owner's go-ahead, then one sweep through `llmops-monitor-sweep`.
- Why the harness registry omits the inline functions (`available_tools=['shell','skills']`). That
  is platform behaviour, observed and not explained; this change makes the driver robust to it.
- `finops/reconcile` shows the same signature (16 recovered of 93 serviced). Fixes 1–4 are in the
  shared loop and cover it; its outcome row is not wired to item 5.
