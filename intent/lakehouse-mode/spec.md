# Spec: lakehouse pipeline modes

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. **State machine.** `PipelineModeChoice` routes `lakehouse_ingest` → `LakehouseGenerate` and
   `lakehouse` → `LakehouseExport`; `data_audit` stays the first choice. Five new states use
   `arn:aws:states:::glue:startJobRun.sync`:
   - `LakehouseGenerate` → `LakehouseLand` → `LakehouseSilver` → `LakehouseGold` → `Complete`
     (the ingest path);
   - `LakehouseExport` → `LakehouseReview` → `FinetuneLaunch` (the training path).

   Each Glue state:
   - has `ResultPath: null` and a `TimeoutSeconds` under the Glue job's own limit;
   - retries only `Glue.ConcurrentRunsExceededException` (a slot still held by a finished run, PR-08);
   - catches `States.ALL` into `EscalateFail` with `ResultPath: "$.error"`.

   Per-run arguments are built with `States.Format` from `$.lakehouse`, using the same layout as
   `deploy/10_lakehouse.py`; a test derives one from the other. `LakehouseReview` is a driver task:
   stage `data-prep`, task `lakehouse_review`, `TimeoutSeconds` 86400.
2. **Execution input.** start_pipeline adds `lakehouse`: `{dataset, data_bucket, seed, scale}` for
   `lakehouse_ingest` (dataset = the lowercased run id), `{dataset, data_bucket}` for `lakehouse`
   (dataset = `params.lakehouse_dataset`), and `null` otherwise. Every state-replacing Pass carries it.
3. **Dispatch rules.**
   - `MODE_REQUIRED_PARAMS`: `lakehouse_ingest` needs `generator_seed` and `generator_scale`;
     `lakehouse` needs `lakehouse_dataset`.
   - `MODE_REQUIRED_ROLES`: `lakehouse` needs `student`.
   - `MODE_REQUIRED_CHOSEN`: `lakehouse` needs `gates` and `dataset`.
   - Dispatch refuses a dataset id outside `^[a-z0-9][a-z0-9-]{0,79}$`, a non-integer seed, and a
     scale the generator does not define.
4. **Generator job.** `llmops-lakehouse-generate` is a Glue Python-shell job running
   `pipeline/lakehouse/generate.py`; its digest is identical on Python 3.9 and 3.12. It writes
   `lakehouse/landing/<dataset>/` and refuses a non-empty prefix. It runs as a role of its own,
   `llmops-lakehouse-generator`, with PutObject on landing only: the transforms' role still cannot
   write its own input. Static job arguments move into each job's `DefaultArguments`. `--smoke` now
   generates through this job.
5. **The review.** The data-prep task `lakehouse_review`:
   - reads the export, Gold and Silver reports;
   - escalates on any failed DQ outcome, plant check or row mismatch;
   - checks overlap with named eval sets, and escalates rather than dropping rows from a pinned
     extract;
   - writes `distillation/stats.json` with `source: "lakehouse"`;
   - claims `extract_uri` / `extract_sha256` / `extract_snapshot_id`, so the driver re-hashes the
     extract.

   `STAGE_EVENT_MAP[("data-prep","lakehouse_review")]` is `DATASET_CURATED`. In lakehouse mode,
   finetune uses the extract's train/val files verbatim as its channels, and eval falls back to
   `val_uri`.
6. **Grants.**
   - SFN role: `glue:StartJobRun`, `GetJobRun`, `GetJobRuns` and `BatchStopJobRun` on
     `job/llmops-lakehouse-*`.
   - Harness role: Get-only on `lakehouse/reports/*` and `lakehouse/extracts/*`, plus the matching
     List prefixes. No Glue, Athena, s3tables or KMS.
   - SageMaker role: Get on `lakehouse/extracts/*`.

   Each grant is verified with `simulate_principal_policy` as that role.
