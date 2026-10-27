# VERIFICATION — the provenance card on a registered model (2026-10-24)

## What the owner asked for

"先做溯源卡": the evidence of a run, on the model version the approver opens in SageMaker Unified
Studio. The product focus, in the owner's words: data + AI governance.

## Where it goes, measured

The owner opened the 2026-10-23 demo run's model version in SMUS (Models → My models →
`llmops-student-store-copilot-lakehouse` → Version 1). The page showed a description, empty
Performance metrics, empty Custom metrics, empty Artifacts, empty Hyperparameters, and container and
version details. None of the ten `CustomerMetadataProperties` the deploy agent had written (run id,
scores, endpoint, teacher and student) appeared anywhere. Per the SageMaker developer guide, every
model version carries a simplified model card that Studio renders from, so the card is the field.

## Shapes found while building it

Read from S3, run by run:
- **`gate.json`, three shapes:** `gate_results` with `wilson95_ci_*` and `n` (2026-10-16 trained run),
  `decisions` with `ci_*` and `counts` (2026-10-23 baseline), `results` with `ci_*` and `count`
  (2026-10-23 demo run).
- **`training/metrics.json`, two shapes:** `training_job` + `loss_curve_assessment` (2026-10-16),
  `job` + `health_assessment` (2026-10-23).

The console's reader knew the first two gate shapes and the first metrics shape, so the Outcome tab
had no tuned scores, instance or billable seconds for the demo run. It now reads through the card's
module.

## Pre-deploy, read-only

- **The builder on the demo run's real artifacts:** a 6 KB card; 4 metrics, 17 hyperparameters,
  evidence for every file read.
- **`simulate_custom_policy` on the new role, 7/7:**
  - `UpdateModelPackage` on a `llmops-student-*` package: allowed;
  - the same call with `sagemaker:ModelApprovalStatus` set: explicitDeny;
  - another group: implicitDeny;
  - S3 put under `runs/*/provenance/`: allowed, elsewhere: implicitDeny;
  - `customer-data/`: implicitDeny; `DeleteModelPackage`: implicitDeny.

## Deployed (2026-10-24, from branch `claude-and-human/provenance-card`)

No pipeline execution was RUNNING.

| Step | Result |
|---|---|
| `01_iam.py --dry-run` | one `[create]` (`llmops-lambda-publish_provenance`); all ten other roles "no change" on trust and inline policy |
| `01_iam.py` | role created, SSM `/llmops/iam/lambda_publish_provenance_arn` |
| `07_lambdas.py --only provenance --only state_machine --dry-run` | ASL `OK`, no diagnostics |
| `07_lambdas.py --only provenance --only state_machine` | `llmops-publish-provenance` created; state machine `definition_confirmed`, `states_live: 39` |

## Live: the backfill, through the pipeline's own Lambda

`llmops-publish-provenance` invoked with `{"run_id": "run-20261023T160741Z-65fec5d8"}`:
`published: true`, `confirmed: true` (the package read back holds the card written),
`card_sha256 2bbadf99…`, gate shape `results`, 4 metrics, 17 hyperparameters, cost **$0.45** (the
run's three SageMaker jobs by billable seconds: probe, full and eval inference).

Read back with `DescribeModelPackage`:
- `ModelApprovalStatus`: **PendingManualApproval**, unchanged.
- `ModelCard.ModelCardStatus`: `PendingReview`.
- Custom metrics: `gate.answer_match 0.725`, `gate.format_validity 1.0`, `train.final_train_loss`,
  `train.final_eval_loss`.
- 2 training datasets (the Gold extracts) and 17 hyperparameters.
- `CustomerMetadataProperties` gained `provenance_card_uri` and `provenance_card_sha256`; the ten the
  deploy agent wrote are untouched.

## Live: the first card was not on the SMUS page, and a probe found the fields that are

After the backfill the owner refreshed the SMUS version page: "Last modified" moved to the write's
minute, and Custom metrics, Artifacts and Hyperparameters were still empty. The developer guide's
schema had been read as "Studio renders the card"; for SMUS that was wrong about which fields.

**What the page reads (CloudTrail, as the project role, on refresh):** `Search` (ModelPackage),
`DescribeModelPackage` on the version, `ListArtifacts` and `ListAssociations` (lineage, the
Deployments tab). No `DescribeTrainingJob`.

**The probe** (the owner's terminal, outside the pipeline as a UI probe, then deleted): a throwaway
group `llmops-probe-smus-render` with one version whose every candidate field carried a value naming
its own source. On its SMUS page:

| Section | Rendered | Not rendered |
|---|---|---|
| Custom metrics | card `training_job_details.training_metrics` | `user_provided_training_metrics`, `evaluation_details`, ModelMetrics statistics |
| Hyperparameters | card `training_job_details.hyper_parameters` | `user_provided_hyper_parameters` |
| Performance metrics | nothing (a ModelQuality statistics file with accuracy/precision/recall/f1 did not fill it) | |
| Artifacts | nothing (`training_datasets`, `model_artifact` did not fill it) | |
| — | | `MetadataProperties`, `CustomerMetadataProperties` |

The page shows a metric's name and score, never its notes. So the builder now writes
`training_metrics` and `hyper_parameters`, and the interval, the threshold, PII redactions and cost
are rows of their own.

## Live: a second write to the same package

The re-backfill failed: `ValidationException: Both ModelCardContent and ModelCardStatus are provided.
Cannot determine which to use.` The same call had succeeded on the first write, when the package had
no card. The publisher now sends the status only on a package's first card, and refuses an
`Archived` card; the test double raises exactly this error, because a double that accepted it is how
the failure reached production.

## Live: the card on the SMUS page

`llmops-publish-provenance` redeployed and invoked again for `run-20261023T160741Z-65fec5d8`:
`published: true`, `confirmed: true`, 12 metrics, 17 hyperparameters. Approval still
**PendingManualApproval**; card `PendingReview`. The owner's refresh of the version page then showed:

- **Custom metrics, 12 rows:** `gate.answer_match` 0.725, `.wilson95_low` 0.63907, `.wilson95_high`
  0.796971, `.threshold` 0.6; `gate.format_validity` 1, 0.968981, 1, 0.95; `train.final_train_loss`
  0.2447; `train.final_eval_loss` 0.2941; `data.pii_redactions` 30; `run.cost_usd` 0.45.
- **Hyperparameters, 17 rows:** base model and its 40-hex revision, max length, learning rate,
  epochs, steps, LoRA alpha/dropout/r, and for train, eval and acceptance the extract's sha256, rows
  and Gold snapshot id.

## Live: the console's Outcome tab, after `deploy/console/deploy.sh`

The deployed package was downloaded and compared: `provenance.py` byte-equal to the branch, and the
live `lambda_function.py` reads gates through `provenance.gate_view`. The owner's reading of the
Outcome tab for `run-20261023T160741Z-65fec5d8`: gate PASSED; answer match 72.5% (CI 63.9%–79.7%,
gate ≥ 60.0%, n=120); format validity 100.0% (96.9%–100.0%); training ml.g5.2xlarge, 408 s billed,
loss curve healthy; cost $0.45, the same figure as the card. Before the change the page had none of
the tuned scores or the job details for this run.

The same reading showed two more defects, fixed in a second commit:
- **"no evaluated answers for this run":** a third file whose shape drifted. The demo run wrote
  `evaluation/student_answers.jsonl` (`item_id`, `student_answer`) and judged with
  `answer_match: "True"`. The 2026-10-16 run wrote `evaluation/i0/predictions.jsonl` (`prompt_id`,
  `answer`) and `verdict: "match"`. Both shapes are now read through `provenance.prediction_view` /
  `judgement_view`.
- **"3 SageMaker job(s), incl. the baseline"** on a run with no baseline: the clause now appears only
  when there is one.

## Live: the Outcome tab after the second commit's console deploy

The deployed package was again compared byte for byte (`provenance.py`, `lambda_function.py`,
`frontend.html`: all equal to the branch). The owner's reading for `run-20261023T160741Z-65fec5d8`:
- **Cost:** "3 SageMaker job(s) · endpoint hours + tokens: Cost tab", with no baseline clause.
- **Answers:** 12 questions with the fine-tuned answer and its verdict (8 match, 4 no_match among
  the 12 shown); the untrained column shows "–" because the run has no baseline.

**Observed in those answers:** every fine-tuned answer ends in a case number the question never gave
("（案件編號 C00089）", "(case C00148)"), and `answer_match` (reference containment) still scores it a
match. Taken up as its own change: `deploy/evidence/VERIFICATION_identifier_grounding.md`. (Recorded
here with that change, because this paragraph's own commit did not reach `main` before #182 merged.)

## Not yet verified live
- `PublishProvenance` inside a fresh pipeline run (deployed; the next run that deploys exercises it).
- Artifacts and Performance metrics on the SMUS page: no field tried fills them.
