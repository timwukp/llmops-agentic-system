# Plan: build-then-train lakehouse runs, and the full pipeline view

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** 8ca377a5dbf39adf735a954f145ef18f5726264a
- **Status:** accepted

`Accepted-for` is the merge base with `main` (PR #179's merge).

## Files changed

1. `orchestration/start_pipeline/handler.py`: either-or lakehouse prerequisites, build dataset id,
   `lakehouse` input object.
2. `orchestration/state_machine.asl.json`: the `PipelineModeChoice` branch and
   `LakehouseThenTrainChoice`.
3. `deploy/console/lambda_function.py`: per-mode stage flows, Glue state cards, the
   `lakehouse-e2e` start preset, `outcome()` and `/api/outcome`. `deploy/console/frontend.html`: the
   button, the card subtitles and the Outcome tab. `deploy/console/README.md`: the tab table.
   `agents/eval/harness.json`: the pinned-baseline clause. `docs/gen_architecture_svg.py` and
   `docs/architecture-console.svg`: the route count.
4. Tests:
   - `tests/test_lakehouse_mode.py`, `tests/test_console_tasks.py`, `tests/test_orchestration.py`,
     `tests/test_console_e2e_demo.py`, `tests/test_console_outcome.py`;
   - `tests/negative_controls/monitor_dispatch.py`;
   - `tests/redaction_scan.py` and `tests/test_redaction_scan.py` (tracked-file count).
5. Docs and records:
   - `docs/ARCHITECTURE.md`, `docs/ARCHITECTURE.zh-TW.md`;
   - `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md`;
   - `PROJECT_STATE.md`, `CHANGELOG.md`;
   - `deploy/evidence/VERIFICATION_m1_lakehouse_e2e.md` (new);
   - `intent/m1-lakehouse-e2e/{intent,spec,plan}.md`, `.sdlc/active`.
