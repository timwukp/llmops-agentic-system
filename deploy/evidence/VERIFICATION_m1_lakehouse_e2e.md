# VERIFICATION — M1: raw customer data to a judged model in one run, its outcome, and the console (2026-10-23)

## What the owner asked for

"完全 end to end from customer raw data to the results/outcome", a pipeline you can see, then (after a
critical review) the outcome first, and a console that is "more professional and 簡潔", after the Kiro
Telemetry dashboard.

## Deployed (2026-10-23, from branch `claude-and-human/m1-lakehouse-e2e`)

The branch sits on `main` `8ca377a` (#179) plus this change, and no pipeline execution was RUNNING
at any deploy.

| Step | Result |
|---|---|
| `07_lambdas.py --only start --only state_machine` (09:57:19Z) | start-pipeline updated. State machine `definition_confirmed`, `states_live: 38`. `audit_drift` beforehand: the only drift was this change (`LakehouseThenTrainChoice` absent, `LakehouseGold.Next`, `PipelineModeChoice`, start-pipeline's handler). |
| `07_lambdas.py --only start` + `05_harnesses.py --agent eval` (10:09:26Z) | start-pipeline: the pinned-baseline check. `llmops_eval` v17, READY, `config_confirmed`. |
| `deploy/console/deploy.sh` (owner's terminal; the sandbox cannot reach PyPI with a trusted certificate) | Dashboard updated: vendored boto3 1.43.118, 29 MB package. |

## Live: the untrained baseline, through the pipeline

`run-20261023T101023Z-5e8b449b`, dispatched through the start-pipeline Lambda as an `eval_only` run:
- **Model:** `hf://Qwen/Qwen3-1.7B@70d244cc86ccca08cf5af4e1e306ecf908b1ad5e`, the revision the
  2026-10-16 run trained from.
- **Prompts:** that run's 120-item held-out acceptance set (`sha256 72253cab…`).
- **Gates and source:** the same gates; `source_run_id` = `run-20261016T012010Z-9de45cbf`.

**Path:** EvalGenerate → EvalScore → EvalGate → Complete, **SUCCEEDED** at 10:30:58Z. The eval agent
handed the inference job off with `job_launched` at about 10:16Z, and the job billed 483 s on
ml.g5.xlarge. Its report records `baseline: true` and `base_model`.

| Same 120 held-out questions | Untrained Qwen3-1.7B | After QLoRA fine-tuning (`run-20261016T012010Z-9de45cbf`) |
|---|---|---|
| answer_match | **0.0%** (0/120; Wilson 95% upper bound 3.1%) | **72.5%** (87/120; [63.9%, 79.7%]) |
| format_validity | **39.2%** (47/120; [30.9%, 48.1%]) | **100%** (120/120; [96.9%, 100%]) |
| gate (answer ≥ 0.6, format ≥ 0.95) | fail, decisive | **pass**, decisive |

**How to read it:** answer_match counts an answer only if it contains the reference verbatim. The 0%
therefore also says the untrained model never learned this store's wording, not only that it cannot
answer. format_validity is the fairer comparison. Per item, 87 questions moved from a miss to a match
and none moved the other way.

## Live: the Outcome tab

Read from the deployed console for `run-20261016T012010Z-9de45cbf`:
- **Gate:** PASSED.
- **Scores, untrained → fine-tuned:** answer match 0.0% → 72.5% and format validity 39.2% → 100%,
  each with its interval.
- **Cost:** $0.60 across 4 SageMaker jobs (training, eval inference and the baseline).
- **Data journey:** 5.2K raw records → Bronze (4 tables) → Silver (283 pairs, 30 PII redactions,
  duplicates 18, 8/8 planted defects caught exactly) → Gold (230 / 23, data quality 18/18) →
  sha256-named extract.
- **Answers side by side,** the changed questions first.

**Found by reading the live page:**
- **The baseline's `gate.json` had a different shape** (`decisions`, not `gate_results`).
- **The trained run's `metrics.json` is wrapped** (`trainer_metrics` beside `training_job`). On the
  first deploy the Training card showed "0 rows".

Both are agent-written files whose shape varies between runs, so the reader accepts both shapes, and
`tests/test_console_outcome.py` holds the live shapes.

## Live: the one-click build-then-train run, from the console button

`run-20261023T160741Z-65fec5d8`, `trigger_source: console-demo`, generator seed 71659, started
16:07:41Z 2026-10-23 by the owner's click, **SUCCEEDED** at 00:08:18Z (8 h 0 min wall clock, most of it
the SageMaker jobs and their queues). Path, read from the execution history:

PipelineModeChoice → LakehouseGenerate → LakehouseLand → LakehouseSilver → LakehouseGold →
LakehouseThenTrainChoice → LakehouseExport → LakehouseReview → FinetuneLaunch → FinetuneLaunchFull →
FinetuneAnalyze → EvalGenerate → EvalScore → EvalGate → EvalOnlyStopChoice → QualityGateChoice →
Deploy → SmokeTest → MonitorHealth → Teardown → MonitorReport → Complete.

| | This run |
|---|---|
| Data | Silver 283 pairs, 30 PII redactions; Gold 234 train / 19 eval, its own dataset `run-20261023t160741z-65fec5d8` |
| answer_match | **72.5%** (87/120; Wilson 95% [63.9%, 79.7%]), decisive pass at 0.6 |
| format_validity | **100%** (120/120; [96.9%, 100%]), decisive pass at 0.95 |
| Model | registered in the SageMaker Model Registry; visible in SageMaker Unified Studio Discover as `llmops-student-store-copilot-lakehouse` |

**The same 87/120 as the 2026-10-16 run is a coincidence, checked rather than assumed:** the eval set
(`sha256 bce1c370…` vs `72253cab…`), the model artifact (this run's own `training/` prefix) and the
inference job all differ between the two runs.

## Not yet verified live

- **The console after the training-card, `eval_only`-flow and flow-colour fixes** shipped with #180;
  re-read on the live console, not separately recorded here.
