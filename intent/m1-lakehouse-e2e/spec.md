# Spec: build-then-train lakehouse runs, and the full pipeline view

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. **Dispatch** (`orchestration/start_pipeline/handler.py`). A `lakehouse` run takes EITHER
   `lakehouse_dataset` (train on an existing dataset, as today) OR `generator_seed` +
   `generator_scale` (build first).
   - Both, or neither, is refused at dispatch.
   - A build run's dataset id is its run id, lowercased, exactly as `lakehouse_ingest` names it.
   - It is written to `params.lakehouse_dataset`, so every agent prompt reads the same field it
     reads today.
   - The execution input's `lakehouse` object carries `seed` and `scale`, which is what the
     state machine routes on.
2. **State machine.**
   - `PipelineModeChoice` sends a `lakehouse` run whose input carries `lakehouse.seed` to
     `LakehouseGenerate`.
   - `LakehouseGold` goes to a new Choice, `LakehouseThenTrainChoice`: a `lakehouse` run
     continues to `LakehouseExport`, anything else (`lakehouse_ingest`) to `Complete`.
   - No other state changes.
3. **Console.**
   - `/api/pipeline` returns the stage list for the execution's own mode:
     - a build-then-train run: Raw → Bronze → Silver → Gold → Extract → Review → Finetune ·
       Launch → Finetune · Analyze → Eval Gate → Deploy → Smoke Test → Teardown → Complete;
     - a `lakehouse_ingest` run: through Gold, then Complete;
     - a train-only `lakehouse` run: from Extract;
     - every other mode: as today.
   - The five Glue states map to those cards, and each card's hover shows its Glue job.
   - `/api/start-run` accepts `{"preset": "lakehouse-e2e"}`, which dispatches a build-then-train run
     with these settings: scale demo, student `Qwen/Qwen3-1.7B`, gates
     `{format_validity: 0.95, answer_match: 0.6}`, dataset label `store-copilot-lakehouse`, and a
     seed from the request or the current time.
   - The Pipeline tab has a button for it. The existing launch-budget gate still applies.
4. **Outcome page** (added after the owner's 2026-10-23 review, "先做成果頁"):
   - `GET /api/outcome?run_id=` returns one run's outcome: the data journey (the Data tab's
     reports), training metrics, the gate with Wilson intervals, compute cost, and up to 12 answer
     pairs, with questions the fine-tune changed shown first.
   - It compares the run with its **untrained baseline**: the `eval_only` run whose manifest names
     this run as `source_run_id` and judged an `hf://` model.
   - Every read stays under `runs/` and `lakehouse/reports/`, and the run id is regex-checked.
   - A new **Outcome** tab renders the response.
   - Baseline dispatch: `eval_only` accepts `model_artifact_uri` as an `s3://` artifact or
     `hf://<org>/<repo>@<40-hex sha>`. The eval prompt loads such a model with `from_pretrained` at
     that revision and changes nothing else.
5. **Console shell** (owner request 2026-10-23: "more professional and 簡潔", after the Kiro Telemetry
   dashboard):
   - a fixed left sidebar grouped Get started / Build / Data & models / Observe / Govern;
   - a slim top bar with the page title;
   - neutral dark surfaces and one accent;
   - sentence-case headings;
   - the fleet/runs/executions summary only on Pipeline;
   - a `‹`/`›` toggle that collapses the sidebar to icons only (labels as tooltips) and remembers the choice.

   The tab buttons and panels keep their `data-tab` / `data-tab-panel` contract, which the tests and
   the diagram generator parse.
6. **Tests.**
   - Dispatch: either/both/neither, the dataset id, the `lakehouse` object.
   - ASL routing for all three lakehouse shapes.
   - Console flows and preset.
   - Negative controls; counts re-derived.
7. **Live.** Deploy start-pipeline, the state machine, the eval harness and the console. Run the baseline for `run-20261016T012010Z-9de45cbf`. Start the run from the
   console button, and record the pipeline view and the outcome in
   `deploy/evidence/VERIFICATION_m1_lakehouse_e2e.md`.
