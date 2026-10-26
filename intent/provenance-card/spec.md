# Spec: the provenance card on every registered model

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Where it goes, and why there

Every model version in the SageMaker Model Registry carries a simplified model card
(`ModelPackageModelCard`, schema "Model package model card schema", sagemaker/latest/dg/
model-registry-details.html), and the SageMaker Unified Studio version page renders from it. The
owner's reading of that page (2026-10-24) showed sections for performance and custom metrics,
artifacts and hyperparameters, all empty, and **no** `CustomerMetadataProperties` anywhere: the
metadata the deploy agent writes today is invisible in Studio. `UpdateModelPackage` accepts
`ModelCard`, so the card is written to the package the run already registered.

## Requirements

1. **Builder, pure** (`pipeline/contracts/provenance.py`). `build_card(artifacts)` takes what the run
   wrote (manifest, `training/metrics.json`, `evaluation/report.json`, `evaluation/gate.json`, the
   lakehouse `export.json`, `silver.json`, `gold.json`, an optional baseline, an optional cost list)
   and returns `{"content": <card>, "evidence": [...], "sha256": ...}`:
   - `model_overview`: `model_creator` (the pipeline), `model_artifact` (the training artifact URI).
   - `intended_uses`: purpose (the dataset), risk rating `Unknown` unless a later step sets one.
   - `training_details.training_job_details`:
     - `training_arn` (when the job record has it), `training_datasets` (the Gold extract URIs);
     - `training_metrics`, the field SMUS's Custom metrics renders (measured with a probe version;
       the `user_provided_*` twins are not shown): each gate metric, its Wilson 95% bounds and
       threshold as rows of their own (the page shows no notes), the baseline's value when a
       baseline exists, final train/eval loss, PII redactions, cost;
     - `hyper_parameters`, the field SMUS's Hyperparameters renders: base model, revision, LoRA
       settings, max length, the Gold snapshot ids, the extract sha256s.
   - `training_details.training_observations`: the data journey in one line (Silver pairs, PII
     redactions, Gold train/eval rows, DQ) and the loss verdict.
   - `evaluation_details[0]`: the gate as a metric group (one simple metric per gate metric), its
     observation, the eval set as a dataset.
   - `additional_information.custom_details`: run id, dataset id, cost in USD with its scope, the
     report and gate URIs with their sha256, the card's own builder version.
   - Every string within the schema's limits (`training_observations` 1024, notes 1024, custom
     values 1024, simple-metric string values 63, ≤ 50 metrics, ≤ 100 hyperparameters); only keys
     the schema allows (it is `additionalProperties: false` throughout).
   - Both gate shapes the eval agent writes (`gate_results`/`wilson95_ci_*`, `decisions`/`ci_*`,
     `results`/`ci_*`) read as one, so the card and the console's Outcome tab agree.
2. **Publisher Lambda** (`orchestration/publish_provenance/handler.py`, `llmops-publish-provenance`).
   Input `{run_id}`. Reads the artifacts from S3, finds the run's model package (the
   `llmops-student-*` group package whose `CustomerMetadataProperties.run_id` is this run), finds the
   baseline (an `eval_only` run whose manifest names this run and an `hf://` model), prices the run's
   SageMaker jobs from the rate card, builds the card, writes it with `UpdateModelPackage`
   (`ModelCardStatus: PendingReview` on a package's first card only, since a later update that
   carries the status as well is refused; approval is a human step) plus two metadata properties
   pointing at the card's S3 copy and sha256, writes `runs/<id>/provenance/card.json`, and reads the
   package back to confirm the card is the one written. A run with no package returns
   `{"published": false, "reason": ...}` rather than failing.
3. **State machine.** A Task state `PublishProvenance` (`lambda:invoke`) between `MonitorReport` and
   `Complete`; `MonitorReport`'s success and catch both go to it; its own catch goes to `Complete`
   with the error kept in `$.provenance`. A card that cannot be written never fails a run that
   succeeded.
4. **Backfill** through the same Lambda: invoking it with `{run_id}` for the 2026-10-23 demo run.
5. **IAM.** A role of its own (`lambda_roles.json` `publish_provenance`): S3 reads under `runs/`,
   `lakehouse/reports/`, `finops/rates/`; S3 put under `runs/*/provenance/` only; SageMaker
   list/describe on model packages and training jobs, `UpdateModelPackage` on
   `model-package/llmops-student-*/*` only; DynamoDB `Scan` on the runs table. The state machine
   role may invoke it.
6. **Tests and controls:** the builder against the live artifact shapes (two gate shapes, wrapped
   metrics, with and without a baseline), schema keys and limits, the gate reader agreeing with the
   console's, the Lambda's package lookup and read-back, ASL placement and catch; negative controls;
   counts re-derived.
7. **Live:** deploy, backfill the demo run, and read the card back from the registry and on the
   SMUS page; recorded in `deploy/evidence/VERIFICATION_provenance_card.md`.
