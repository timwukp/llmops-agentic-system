# VERIFICATION — the lakehouse pipeline modes, Phase 1 PR-09 (2026-10-15)

**The design, chosen by the owner.** Step Functions runs the five lakehouse Glue jobs itself
(`glue:startJobRun.sync`), and one data-prep turn reviews the result. The rejected alternative had
agents launch the jobs through an EventBridge wake chain. The jobs' work is fixed by the job and its
arguments, so an agent turn there would decide nothing. It would also cost a model call and a
fleet-wide `glue:StartJobRun` on the role all seven harnesses share.

## Deployed

| What | How | Read back |
|---|---|---|
| IAM: SFN, harness and SageMaker roles | `01_iam.py` | the dry-run diff was exactly the three new grants, with no unrelated drift |
| Generator job + `llmops-lakehouse-generator` role; every job's `DefaultArguments` | `10_lakehouse.py` | `--verify` **59/59** (the previous 48, plus 9 generator-role cases asked of IAM as the role and 2 job checks) |
| State machine, 35 states; start-pipeline; driver | `07_lambdas.py` | `ValidateStateMachineDefinition` OK, no diagnostics; `states_live: 35` |
| data-prep, finetune, eval prompts | `05_harnesses.py` | READY at versions 20 / 15 / 15; each live prompt carries its lakehouse clause |
| Console (the review on the curate card) | `deploy/console/deploy.sh` | deployed |

## The deploy smoke, now through the generator job: 24/24 PASS

The smoke ran `--smoke --seed 7 --scale demo` on dataset `seed7-demo-20261015-055949`. Each job received
only its per-run arguments (`run_arguments`, the layout the ASL mirrors). Everything else had to come
from the job's `DefaultArguments`, so a green smoke proves those were deployed.

| Check | Result |
|---|---|
| generator job (Glue Python shell, Python 3.9) | SUCCEEDED |
| **generator job digest = local digest (3.9 vs 3.12)** | `7a05b9ad…` both |
| land / silver / gold / export ×2 | SUCCEEDED |
| export reproducible; driver re-verifies train and eval | yes |
| 8 rules vs plants; Silver and Gold DQ; Athena counts | 8/8 exact; passed; 283 / 251 / 32 |

## Live pipeline run 1: `lakehouse_ingest` (`run-20261015T061901Z-ff69ff34`): SUCCEEDED

The run was started through the start-pipeline Lambda with
`{pipeline_mode: lakehouse_ingest, generator_seed: 7, generator_scale: demo}`.

| State | Entered | Duration |
|---|---|---|
| LakehouseGenerate | 14:19:02 | 57 s |
| LakehouseLand | 14:19:59 | 2 min 8 s |
| LakehouseSilver | 14:22:07 | 3 min 55 s |
| LakehouseGold | 14:26:02 | 1 min 54 s |
| Complete → MarkRunDone | 14:27:56 | — |

Read back:
- The run row is `completed` with `pipeline_mode: lakehouse_ingest`.
- The dataset is the run id, lowercased: `run-20261015t061901z-ff69ff34`. Its landing digest is
  `7a05b9ad…`, seed 7, demo.
- **8/8 plants caught exactly; Silver DQ 6/6, Gold DQ 10/10;** 283 pairs → 251 / 32.
- No harness was invoked. `MarkTaskDone` failed and its Catch absorbed it; that is the normal path
  for a run with no conductor task.

**Found by reading back.** The run's manifest had no record of which dataset it built: the Glue
states write nothing to it. start-pipeline now writes the same `lakehouse` object into the
manifest that it puts in the execution input. The schema declares it, with the dataset-id
pattern.

## Dispatch refusals, live

A `lakehouse` run naming `../customer-data` as its dataset was refused by the start-pipeline Lambda
with `ValueError: params.lakehouse_dataset must match ^[a-z0-9][a-z0-9-]{0,79}$`. Nothing was
created first: no run id, manifest, run row or `PipelineStarted` event. The other refusals are offline
tests: missing seed or scale, a bool, negative or injected seed, an unknown scale, an upper-case id,
an unnamed student, and inherited gates.

## Live pipeline run 2: `lakehouse` (`run-20261015T065307Z-97655783`)

The run was started with:
- `pipeline_mode: lakehouse`
- `lakehouse_dataset: run-20261015t061901z-ff69ff34`, the dataset run 1 built
- student `Qwen/Qwen3-1.7B`
- gates `{format_validity: 0.95, answer_match: 0.6}`, chosen by the owner

**LakehouseExport** succeeded. **LakehouseReview** (data-prep) wrote `distillation/stats.json` with
`source: "lakehouse"`, both extract URIs, both digests, 251 / 32 rows and both snapshot ids. It
recorded these checks, each `pass`:
- Silver DQ: 6 rules, all Passed.
- Gold DQ: 5 + 5 rules, all Passed.
- Plants: 8/8, each caught = planted.
- Export rows equal Gold rows.
- Both files hash to the recorded digests.

The review claimed `extract_uri` / `extract_sha256` / `extract_snapshot_id`. **The driver's own
attestation:**
`{kind: gold_extract, verified: true, computed_sha256 = claimed_sha256 = 4dffee59…, rows: 251, task: lakehouse_review}`.
That is the same train digest as the two smoke datasets: a third, separately built dataset produced
the same bytes.

**FinetuneLaunch.** The finetune agent launched `llmops-qlora-run-20261015T065307Z-97655783-i0-probe`
(ml.g5.2xlarge). Its `train` and `val` channels are exactly the two extract files, `…/train.jsonl` and
`…/val.jsonl`, not their folder, which also holds the UNLOAD parts. The SageMaker role's read of them is the
new `S3GoldExtractsRead` grant, verified by simulation below; the job has not reached its download
step yet (next paragraph).

**Training status at the time of writing.** The probe has been `Pending` since 15:02 with SageMaker's
own message, *"Training job waiting for capacity"* (ml.g5.2xlarge, us-east-1). Nothing in PR-09 is
involved in that wait. The pipeline's existing capacity handling covers it: a stop with $0 billed is a
capacity relaunch, not a failure, up to `CAPACITY_RETRY_CAP`. The run is left to proceed through the
pipeline rather than nudged by hand. Its training, eval and gate outcome will be recorded when it ends,
as PR-11's live end-to-end evidence if it ends after this PR merges.

## Least privilege

- **SFN role:** gains `glue:StartJobRun`, `GetJobRun`, `GetJobRuns` and `BatchStopJobRun` on
  `job/llmops-lakehouse-*` only, and no `iam:PassRole`, since each job's role is fixed in its
  definition.
- **Harness role:** gains Get-only on `lakehouse/reports/*` and `lakehouse/extracts/*`, plus the
  matching List prefixes. It gets no Glue, Athena, s3tables or KMS: the tables stay readable only by
  the Glue roles. All seven harnesses share the role, so Gold *read* is fleet-wide, and the spec
  accepts that.
- **SageMaker role:** gains Get on `lakehouse/extracts/*`.
- **Generator role:** may write landing and nothing else. The transforms' role still cannot write
  landing, so neither job can rewrite the other's input.
- **Asked of IAM as each role** (`simulate_principal_policy`, **15/15**):
  - SFN: may start and stop the lakehouse jobs. It may NOT start another job, delete a job, or
    PassRole.
  - Harness: may Get extracts and reports. It may NOT Put either, read landing, start a Glue job,
    or `GetTableData`.
  - SageMaker: may Get extracts. It may NOT Put them or read reports.
- **Mutation-checked:** three new negative controls fail their named tests:
  - an ASL argument drifting from the smoke-proven layout;
  - agents gaining write on the extracts;
  - dispatch dropping the value checks.

## Also found

- **The prompt-prefix guard compared only a URI's first path segment.** A two-level grant like
  `lakehouse/reports/` could never satisfy it, short of granting all of `lakehouse/` (the Glue scripts
  and landing included). It now matches whole paths against each granted prefix.
- **ARCHITECTURE said "Four entry modes" while the machine routed six;** `vision` was not documented
  at all. Both languages now name all seven, and a test derives the count from the ASL.
- **Two negative controls were uncaught, found by running all of them** on the build runner and then
  locally (**392/392** locally after the fixes; on the runner, the ffprobe and git-index controls
  depend on tools it lacks):
  - **m189:** the eval-prompt guard matched `fall\s*back` anywhere in the val-split sentence, and
    this PR's lakehouse parenthetical ("the fallback is its val_uri") satisfied it with the ranking
    stripped. It now requires the ranking clause itself.
  - **COST.md budget passage (pre-existing on `main`):** the guard's 1600-character window reached a
    later section's "sweep", so deleting the bullet that names the sweep went unnoticed while the
    documented count read 389/389. The window is now the passage itself.
