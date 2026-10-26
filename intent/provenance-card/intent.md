# Intent: every registered model carries its evidence, where the approver looks

- **Slug:** provenance-card
- **Author:** Claude (AI agent)
- **Date:** 2026-10-24
- **Accepted-by:** Tim WU
- **Status:** accepted

Accepted on the owner's chat decision of 2026-10-24 ("好，兩個都同意，先做溯源卡"), after the research
on what SageMaker Unified Studio and Databricks leave to the customer. The product focus, in the
owner's words: data + AI governance.

## Problem

A model the pipeline registers reaches SageMaker Unified Studio (Discover lists it) with almost none of
the evidence the run produced:
- **What the registry holds today:** a description and a handful of `CustomerMetadataProperties`
  (run id, two scores, endpoint, teacher/student, artifact) that the deploy agent chooses per run in a
  CLI call. No model card, no model metrics, no dataset snapshot or sha256, no PII audit, no baseline,
  no cost.
- **Where the evidence lives instead:** S3 (`runs/<id>/…`, `lakehouse/reports/<dataset>/…`) and the
  console's Outcome tab, neither of which is where an approver or a data steward looks.
- **Why it matters:** the package waits at `PendingManualApproval`; the person approving it sees two
  numbers and a sentence. Researched gaps (deploy/evidence and the 2026-10-24 report): SMUS leaves
  "data snapshot → training run" lineage to user code, and its MLflow → registry sync carried no
  metrics or evaluation until September 2026.

## Outcome wanted

- **The card:** each model package the pipeline registers carries a provenance card, assembled by code
  from the run's own artifacts (never retyped by an agent):
  - the data: dataset id, Gold snapshot, extract sha256, rows, DQ, PII redactions;
  - the training: base model, job, instance, steps, loss verdict;
  - the gate: each metric with its Wilson interval, threshold and verdict; the eval set's sha256;
  - the baseline: the untrained model on the same questions, when one exists;
  - the cost: the run's SageMaker jobs.
- **Where:** the place SMUS reads for a registered model, so it shows on the model's page in Discover.
- **Through the pipeline:** written by a pipeline step after Deploy registers the package, and by the
  same code for an existing run on request, so no card is hand-made.
- **Adapter-shaped:** the card is built once and written by a SageMaker-registry writer; a Unity
  Catalog writer can be added later without touching the builder.

## Acceptance

- Offline guards and negative controls.
- A live pipeline run registers a package whose card shows on its SMUS page, every value traceable to
  an artifact by URI and sha256.
- The 2026-10-23 demo run's package gets the same card by the same code.
