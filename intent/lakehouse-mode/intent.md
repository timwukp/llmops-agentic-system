# Intent: the pipeline runs the lakehouse

- **Slug:** lakehouse-mode
- **Author:** Claude (AI agent)
- **Date:** 2026-10-15
- **Accepted-by:** Tim WU
- **Status:** accepted

## Problem

Phase 1 built the lakehouse (PR-04 storage, PR-06 generator, PR-07 medallion, PR-08 export), but only
the deploy script's `--smoke` runs it. No pipeline run can ingest data into it or train on it, so the
reproducibility claim PR-08 made (a model trained on one Gold snapshot, named by its sha256) has no
run that exercises it. The owner asked to continue with PR-09 after #165 (2026-10-15). They chose
Step Functions running the Glue jobs directly, with one data-prep agent turn that reviews the result,
over agent-launched jobs with an EventBridge wake chain.

## Desired outcome

Two pipeline modes:
- `lakehouse_ingest` generates a dataset and builds Bronze → Silver → Gold, then stops.
- `lakehouse` exports a named Gold dataset at its recorded snapshots, has the data-prep agent review
  the export, and trains on the extract through the existing finetune → eval path.

## Acceptance

- Both modes route only from `PipelineModeChoice`. `lakehouse_ingest` reaches no harness.
  `lakehouse` reaches `FinetuneLaunch` only through the review.
- A failed Glue job, including a failed DQ rule, escalates; it never reaches remediation.
- Dispatch refuses a lakehouse run missing its required params or carrying an invalid dataset id.
- A live `lakehouse_ingest` run completes through Step Functions. A live `lakehouse` run reaches the
  review, and the driver re-verifies the extract the review claims.
