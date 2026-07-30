# Verification — training-job deliverability fixes

Evidence that the fixes in `pipeline/training/train_qlora.py` (PR #6) actually work,
gathered from real SageMaker training jobs before relaunching the 9-hour v2 run.

## Why these fixes existed

Run `llmops-qlora-run-v2-code-distill-0001-e1g6` burned **43 GPU-minutes on
ml.g6.2xlarge and produced zero artifacts.** The training itself was healthy — loss
0.335 and mean token accuracy 0.90 at step 88 — so nothing about the model or data
was at fault. It was thrown away for two purely operational reasons:

| # | Defect | Why it was fatal |
|---|---|---|
| 1 | `save_strategy="epoch"`, `output_dir` under `/tmp` | The only save point was step 1265. At 29 s/it a full epoch needs ~10.2 h, but `MaxRuntimeInSeconds` was 14400 (4 h) → hard kill at step ~496 (39%). The save point was **unreachable by arithmetic**, and `/tmp` is not the `CheckpointConfig` path, so nothing synced to S3 to resume from either. |
| 2 | `requirements.txt` had drifted and dropped `liger-kernel` | The code sets `use_liger_kernel=True`, so the trainer died at startup with `ImportError`. The committed requirements no longer matched the confirmed-green recipe that the live job's own tarball contained. |

## Test matrix — all on real SageMaker GPU instances

| Job | Instance | Purpose | Result |
|---|---|---|---|
| `llmops-qlora-v2-smoke-001` | ml.g6.2xlarge | First smoke of the fixed script | **Failed as intended** — reproduced defect 2 |
| `llmops-qlora-v2-smoke-003g5` | ml.g5.2xlarge | Full happy path after the requirements fix | **Completed** |
| `llmops-qlora-v2-smoke-004budget` | ml.g5.2xlarge | Budget trip (accidentally proved resume instead) | **Completed** |
| `llmops-qlora-v2-smoke-005budget` | ml.g5.2xlarge | Budget trip, fresh checkpoint prefix | **Completed** |

Dataset for all smokes: 200 train / 20 val rows sampled from the real v2 set — same
schema, same tokenizer, same code paths, ~5 minutes per run instead of 9 hours.

### smoke-001 — the cheap job that caught the expensive bug

```
ImportError: You have set `use_liger_kernel` to `True` but liger-kernel >= 0.3.0
is not available. Please install it with `pip install liger-kernel`
```

This is the entire justification for smoke-testing. A ~$0.30 job surfaced a defect
that would otherwise have cost another queue-wait plus startup cycle on the real run.
It also confirmed the new data paths work on real data before training ever started:

```
[data] train: dropped 25/200 rows over 4096 tokens (12.50%)
[data] val: dropped 0/20 rows over 4096 tokens (0.00%)
[data] val: capping 20 -> 8 rows for eval cost
```

Fix: restored the floors-only set verified against
`pytorch-training:2.6.0-gpu-py312-cu126-ubuntu22.04-sagemaker`, and added a preflight
`liger_kernel` check that fires **before** the multi-minute base-model download.

### smoke-003g5 — happy path, and proof checkpoints reach S3 mid-run

```
[save] adapter -> /opt/ml/model/adapter
METRIC train_loss=0.4307   METRIC eval_loss=0.4662   METRIC completed_fraction=1.0
TRAINING_COMPLETE
```

The claim that matters here is not "it trained" but **"checkpoints reached S3 while the
job was still running"** — that is what makes a killed job recoverable. S3 timestamps
prove it, since `checkpoint-3` landed over three minutes before the job ended:

```
00:34:49   checkpoint-3/adapter_model.safetensors    33.3 MiB
00:38:01   checkpoint-11/adapter_model.safetensors   33.3 MiB   <- job ended 00:38
```

Model artifact produced: `smoke/output/.../model.tar.gz` (27.4 MiB).

### smoke-004budget — accidental proof of the resume path

Set `max_train_seconds=60`, but the budget never tripped. The reason is visible in the
metrics and is itself a useful result:

```
"resumed_from": "/opt/ml/checkpoints/checkpoint-11",
"global_step": 11, "max_steps": 11, "budget_stopped": false
```

It reused smoke-003's checkpoint prefix, **auto-resumed from checkpoint-11**, found
training already complete, and went straight to saving. Unintended, but it validates
resume-from-newest-checkpoint end to end on a real job — the exact behaviour that would
have salvaged the e1g6 run.

### smoke-005budget — the budget trip, isolated

Fresh checkpoint prefix, `max_train_seconds=20` (below one step's ~27 s):

```
[budget] 20s training budget reached at step 1/11 — stopping gracefully to save artifacts
[save] adapter -> /opt/ml/model/adapter
METRIC completed_fraction=0.09090909090909091
"budget_stopped": true, "resumed_from": null, "epochs_completed": 0.0909
TRAINING_COMPLETE
```

Job status: **Completed**, not Stopped or Failed. This is the whole point of the change —
a run that ran out of time still saves an adapter, still evaluates, still exits 0, and
**labels itself honestly** as 9% complete so downstream gates can't mistake a partial run
for a finished one.

## Local unit tests (no GPU needed)

`tests/test_training_deliverability.py` — 11 tests, part of the normal suite
(`.venv/bin/python -m pytest tests/ -q` → **41 passed**):

- **The e1g6 config is now a test fixture.** The exact payload that wasted 43 GPU-minutes
  is fed to the validator and must produce failures naming all three defects: no periodic
  saves, no `CheckpointConfig`, and a time limit that reaches only **39%** of the run.
  That 39% is the same number the real job died at.
- A budget `>=` `MaxRuntimeInSeconds` is fatal; thin headroom warns but does not block;
  a partial run warns *only* when checkpoints plus a graceful budget let it save.
- SageMaker's quoted hyperparameters (`'"50"'`) parse correctly — a silent-zero here would
  disable the very checks above.
- `truthy()` — handles quoted booleans (`'"true"'` → `True`).
- `newest_checkpoint()` — numeric sort, so `checkpoint-100` wins over `checkpoint-99`
  and `checkpoint-20`. Lexicographic sorting would resume from the wrong checkpoint;
  this is asserted explicitly. Missing/empty directories return `None` rather than raising.

## Arithmetic for the relaunch — the check that should have run the first time

Measured on g5.2xlarge: **~26.6 s/it** over 11 steps.

| Setting | Value | Reasoning |
|---|---|---|
| Rows after dropping >4096-token samples | ~20,002 of 20,225 | 1.1% dropped, not truncated |
| Steps per epoch | ~1,250 | effective batch 16 (2 × 8) |
| Training time needed | ~9.2 h | 1,250 × 26.6 s |
| `MaxRuntimeInSeconds` | 43,200 (12 h) | **must exceed** the above, unlike e1g6's 4 h |
| `max_train_seconds` | 40,500 (11.25 h) | 45 min headroom for save + eval + merge + upload |
| `save_steps` | 50 | ~25 checkpoints per epoch; worst-case loss is 50 steps |

This arithmetic is no longer done by hand. `pipeline/training/validate_job_config.py` runs
it against the `CreateTrainingJob` payload and exits non-zero on any FAIL, so a launcher —
agent or human — can gate on it:

```
$ python pipeline/training/validate_job_config.py real_job.json --sec-per-it 26.6 --rows 20225
job: llmops-qlora-run-v2-code-distill-0001-e2
  fact  headroom after training budget: 45 min
  fact  ~1264 steps at 26.6s/it needs 9.3h; limit 11.2h reaches step ~1522 (120% of the run)
  fact  ~25 checkpoints planned; worst-case loss 50.0 steps
PASS — safe to launch
```

Run against the e1g6 payload it fails with three named defects — see the unit tests above,
where that payload is a permanent regression fixture.

Relaunched as **`llmops-qlora-run-v2-code-distill-0001-e2`**, which passed the gate above
before `create-training-job` was called.

## Lessons recorded

1. **Verify a long job's save points are reachable within its own runtime limit** before
   launching. This was catchable with multiplication, not debugging.
2. **Smoke-test at small scale first.** ~5 minutes and pocket change caught a dependency
   defect that would have wasted hours.
3. **The repo copy of a script can drift from the code that actually works.** The live
   tarball had the OOM fix and correct dependency floors; the committed version had
   neither. When a job is running, diff the S3 sourcedir against the repo before trusting
   either — then commit the working version back.
4. **A partially-trained model is a deliverable; a killed job is not.** Prefer graceful
   degradation with honest metadata over all-or-nothing runs.
