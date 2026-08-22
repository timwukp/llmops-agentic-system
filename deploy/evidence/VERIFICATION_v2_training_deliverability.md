# Verification — training-job deliverability fixes

Evidence that the fixes in the canonical QLoRA trainer (PR #6) actually work, gathered from
real SageMaker training jobs before relaunching the 9-hour v2 run.

> **Paths in this document are historical.** The trainer and the preflight verified below
> were at `pipeline/training/train_qlora.py` and `pipeline/training/validate_job_config.py`
> when this evidence was gathered. Both now live in `pipeline/training/distill/` — the one
> directory `deploy/03_storage.py ensure_code()` mirrors to `s3://<bucket>/code/distill/`
> and the only one a training job can read. The move is the whole point of the fix that
> followed: for months these bytes were verified, unit-tested, and mirrored NOWHERE, while
> a second copy under `distill/` — the one every run actually downloaded — carried
> `save_strategy="no"` and none of the three rules below. The verification results are
> unchanged; the file that carries them is now the file that runs.

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

`tests/test_training_deliverability.py` — 23 tests, part of the normal suite. The suite total
lives in `docs/TEST_RESULTS.md`, which is checked against `pytest --collect-only` on every PR;
it is deliberately not restated here, because it was — this line read "11 tests … **41
passed**" for months after the module reached 23 and the suite passed 1,588, and nothing was
red, since the count guard read only `docs/TEST_RESULTS*.md`. The per-module counts in this
file are now derived too (`tests/test_docs_claims.py::test_readme_claims_about_a_test_modules_size_are_derived`).

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
| Rows after dropping >4096-token samples | ~20,002 of 20,225 | 1.1% dropped, not truncated — **wrong, see below** |
| Steps per epoch | ~1,250 | effective batch 16 (2 × 8) |
| Training time needed | ~9.2 h | 1,250 × 26.6 s |
| `MaxRuntimeInSeconds` | 43,200 (12 h) | **must exceed** the above, unlike e1g6's 4 h |
| `max_train_seconds` | 40,500 (11.25 h) | 45 min headroom for save + eval + merge + upload |
| `save_steps` | 50 | ~25 checkpoints per epoch; worst-case loss is 50 steps |

**Correction, measured later:** the 1.1% in the first row is an artifact of sizing rows with
the `chars / 4` rule of thumb. Tokenized with the real Qwen3 tokenizer, `max_length 4096`
drops **3,603 of 20,225 rows (17.8%)** — sixteen points more, and 131 of 809 source tasks
disappear entirely (`pipeline/v2/README.md` carries the derivation). The row is left as
written because this file records what was believed at launch time, and the belief is the
point: every downstream figure in the table inherits it, so "steps per epoch ~1,250" was
really ~1,039. The direction is conservative — an overstated row count overstates the wall
clock, so the runtime the job was given was larger than it needed, not smaller. It is still
a 16-point error in the one input the rest of the table multiplies, which is why the
relaunch arithmetic is now a script fed a measured `--rows` instead of an estimate.

This arithmetic is no longer done by hand. `validate_job_config.py` (now
`pipeline/training/distill/`, mirrored to `code/distill/` beside the trainer) runs it against
the `CreateTrainingJob` payload and exits non-zero on any FAIL, so a launcher — agent or
human — can gate on it. Measured after the fact: for its entire life at the old path it had
**zero callers**, and 4 of 4 real jobs launched with `save_steps`, `max_train_seconds` and
`CheckpointConfig.S3Uri` all unset — both of its hard FAILs, on every job. The
`agents/finetune` launch bullet now names it and forbids launching a FAILing payload.

```
$ python validate_job_config.py real_job.json --sec-per-it 26.6 --rows 20225
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

<!-- PREFLIGHT-MEASUREMENT-SECTION -->
## The input that arithmetic needs, and that nothing measured

Everything above multiplies `sec_per_it`. Read the command line again:

```
$ python validate_job_config.py real_job.json --sec-per-it 26.6 --rows 20225
```

26.6 was measured **once**, over 11 steps, on `ml.g5.2xlarge`, at `max_length 4096`, on a
1.7B student. The run being sized now is a 4B student at `max_length 14336` — 3.5× the
sequence length, where attention is superlinear and the activation memory that
`gradient_checkpointing` trades away scales with it. 26.6 is not a stale estimate for that
job; it is a number from a different experiment. And the flag had a **default of 27.0** —
rounded from that same measurement, in a help string naming the instance and model it came
from — so a launcher that forgot `--sec-per-it` got it silently.

Worse, and only visible on re-reading the source: the block that multiplies it was wrapped in
`if rows and sec_per_it and max_runtime:`, so a missing input **skipped** the check. Nothing
else in the script fails a well-formed payload, so the one tool written to stop an
unaffordable launch printed `PASS — safe to launch` for anyone who forgot a flag. Measured
after the fact, that was every launch it ever had: zero callers for its whole life at the old
path. Both inputs are now required — absent, they are named FAILs that say how to obtain the
number — and there is no default to fall back on.

Peak VRAM was worse still: recorded nowhere at all, in any of the five real jobs. "Does
`max_length 14336` fit on this instance" had no answer short of a job that OOMs an hour in —
and an OOM an hour in looks exactly like a healthy first twenty minutes.

So the trainer can now be asked for both, before a long run is signed:

| Knob / field | What it is for |
|---|---|
| `--max_steps N` | Stop after N **optimizer** steps. `--max_train_seconds` cannot do this — a wall-clock budget returns whatever step count it returns, and the step count is the quantity being measured. |
| `metrics.json` → `sec_per_step` | `mean`, `p50`, `p90` over all steps **and** `p50_steady` / `p90_steady` with the first 3 excluded, plus `first_step` itself. |
| `metrics.json` → `peak_vram` | `max_allocated_gib`, `max_reserved_gib`, `device_total_gib`, `reserved_fraction_of_device` — recorded twice, for `training` alone and `including_eval`. |
| `metrics.json` → `intended_steps_full_run`, `step_capped` | What the full run would take, and whether this run was a probe. |

Three things in that table are there because the obvious version of each is wrong:

**The unit is the optimizer step, not the micro-batch.** `on_step_end` fires once per
optimizer step, after all `gradient_accumulation` micro-batches — the same unit
`validate_job_config.py` multiplies (`steps = rows × epochs / (batch × accum)`). Timing
micro-batches instead would report a number 8× too small at `gradient_accumulation=8`, pass
every sanity check, and produce a runtime estimate an eighth of the truth. The trainer and the
validator now derive the step count from the same arithmetic, and a test compares them, because
two producers of one number is how the mirror-integrity check came to verify zero files.

**The mean is the wrong statistic over 20 steps.** `train_runtime / global_step` — the only
speed number any previous run recorded — folds in the first steps' CUDA autotune and allocator
growth. Over 2,525 steps that is noise. Over the 20 a preflight can afford it *is* the
measurement: a 90 s first step against a steady 11 s puts the mean at 16 s, 45% high, which
over a full epoch asks for 3.5 hours the run will not use. Hence `p50_steady`, with
`first_step` reported beside it so a first step wildly above the rest shows that the excluded
window was too small.

**`reserved`, not `allocated`.** The number that OOMs is the allocator's pool, not the live
tensors. A snapshot keyed on `max_memory_allocated` would have called a job at 21 of 24 GiB
reserved "8 GiB, roomy".

And one honest-labelling fix. `--max_steps 20` sets `trainer.state.max_steps` to 20, so
`global_step / max_steps` is **1.0** and a 20-step probe writes `"completed_fraction": 1.0`
into `metrics.json` — the one field every gate in this repo reads to tell a partial run from a
finished one, and the field the graceful-budget work above exists to make trustworthy. A capped
run is now measured against the full run instead (20 of 2,525 = 0.79%), it records
`step_capped: true`, and it prints `[probe] STEP-CAPPED RUN … this is a MEASUREMENT, not a
trained adapter` next to the `TRAINING_COMPLETE` that a capped run legitimately still emits.

`tests/test_preflight_measurement.py` — 21 tests, no GPU, no AWS. Each guard was confirmed by
mutating the production code in **both** copies of the trainer until a named test dies, with a
no-mutant control and a sha256 check on every restored file:

| Mutation | Tests that die |
|---|---|
| *no mutant (control)* | **0** |
| drop `gradient_accumulation` from the step count | 5 |
| label a capped probe `global_step / max_steps` | 1 |
| include the warmup steps in the steady-state median | 1 |
| floor the step count instead of ceiling it | 1 |
| key peak VRAM on live tensors, not the allocator pool | 1 |
| return `None` instead of a reason when there is no GPU | 1 |
| drop the step-capped warning from the log | 1 |
| read the training VRAM mark after `evaluate()` | 1 |
| never pass `--max_steps` to the trainer config | 1 |
| stop registering the step timer | 1 |
| drop `sec_per_step` from `metrics.json` | 1 |
| mislabel the unit as a micro-batch | 1 |

Twelve mutants, no survivors.

The same treatment for the preflight's own fixes, over both copies of
`validate_job_config.py` (`~/Documents/llmops-evidence/arc2-v2/mutations_validator.result.json`):

| Mutation | Tests that die |
|---|---|
| *no mutant (control)* | **0** |
| restore the 27.0 default for `--sec-per-it` | 1 |
| skip the core check when `--rows` is missing (the old behaviour) | 1 |
| skip the core check when `--sec-per-it` is missing | 1 |
| name the missing input without naming the probe that measures it | 1 |
| floor the step count instead of ceiling it | 1 |

The last row is why the two-producer test now asserts **exact** equality rather than `abs(n -
mine) <= 1`. That tolerance was wide enough to cover the very defect it was meant to catch:
the validator floored while the trainer ceiled, and on any shape where they disagree they
disagree by exactly one. A tolerance the size of the bug is not a comparison.

The harness itself had a defect worth recording, because it produced a plausible table:
`line.split(" ")[0]` on pytest's `FAILED tests/x.py::name - …` yields the literal string
`FAILED` for every line, so the set of dead test names had size 1 no matter how many died, and
every row read "1 died" **by construction**. Field 1, not field 0. The rows above are the
re-run, with each mutation's dead test named; the trainer table above it came from a different
harness whose counts vary (0, 5, 1, …), which is the shape this bug cannot produce.

Two of those mutants survived the first draft of the tests and both were real gaps, worth
recording because neither was a typo. The steady-state fixture was 17 samples of 11.0 s out of
20, so its median was 11.0 whether or not the warmup was excluded — an implementation that
never excluded anything passed it, output byte-identical. And the production shape divides
exactly (20,200 / 8 = 2,525), so `ceil` and `floor` agreed on every case the tests used; a
floor understates the step count, which understates the wall clock, which is the direction
that killed e1g6.

### The gate did not read the flag its own usage line names

Found by running the real probe payload through it, which is the only reason it was found:
the preflight reported `~75 steps at 30.0s/it` for a payload configured with `--max_steps 20`.
The arithmetic reads rows, epochs, batch size and accumulation; `max_steps` was not one of them,
so it sized the job the payload would have been *without* the cap. The direction is conservative
for a probe — it over-estimates, so a capped run cannot be refused for a limit it clears — but
the `fact` line is the entire output an operator reads, `~N checkpoints planned` is derived from
the same N, and a capped run that PASSES said nothing about the uncapped run it exists to
measure. A guard's scope is a claim about where the defect cannot be.

The gate now sizes `min(cap, full_pass)` and, when a cap binds, warns with **both** numbers and
states that the verdict does not transfer:

```
fact  ~20 steps at 30.0s/it needs 0.2h; limit 0.7h reaches step ~80 (400% of the run)
WARN  --max_steps 20 caps this job at 20 of 75 steps (26.7% of a full pass): this payload
      is a MEASUREMENT, and a PASS here says nothing about the uncapped run it sizes.
```

Five mutants over both copies, no survivors
(`mutations_validator_maxsteps.result.json`):

| Mutation | Tests that die |
|---|---|
| *no mutant (control)* | **0** |
| ignore `--max_steps` entirely (the defect, exactly as found) | 2 |
| read the cap but never let it bind | 2 |
| let a cap above the full pass invent steps (`min` → `max`) | 2 |
| size the cap correctly but stay silent that a PASS does not transfer | 1 |
| state the cap without the full-pass number to compare it against | 1 |

Renaming `steps` to `full_steps` also broke the anchor of an earlier mutation in
`mutations_validator.json`, which the harness caught as `anchor appears 0x, expected 1` rather
than silently reporting a survivor. A mutation spec is code: it goes stale when the code it
names moves, and a spec that no longer applies is a row of evidence that quietly stops
existing.

### What the corpus measures, on CPU, for $0

Before spending a GPU minute, the plan's `dropped == 0` requirement was settled locally by
tokenising every row with the **mirror's own tokenizer files** — the same bytes that will train,
pulled from `models/qwen3-4b-thinking/` and sha256-checked against `MODEL_MANIFEST.json`
(6 of 6 non-weight files match) — rather than assuming the Qwen3 sizes share a tokenizer:

| Split | rows | p50 | p90 | p99 | max | over `max_length 14336` |
|---|---|---|---|---|---|---|
| train | 20,200 | 2,318 | 5,214 | 9,538 | **12,981** | **0** |
| val | 1,000 | 2,403 | 5,723 | 11,958 | 11,962 | **0** |

So `--drop_overlong true` is expected to drop nothing, and a non-zero `dropped` in the job's
own `metrics.json` — which stays the authority — is now a stop-and-look rather than a surprise.
Local `transformers` 5.6.2; `corpus_tokens.json` in the evidence directory.

That census also decides how the probe is *cut*, because one dataset cannot measure both
numbers honestly. `per_device_batch_size` is 1 with packing off, so the allocator high-water
mark is set by the longest row the run ever touches, while seconds per step is an average over
the length distribution. Twenty steps consume 160 rows out of 20,200 — a random sample will
almost certainly never draw a p99 row, so a representative probe reporting "fits with room" is
evidence about the median row and silence about the row that OOMs at hour six. Hence two
probes, two claims:

| Probe | Dataset | p50 tokens | max tokens | Answers |
|---|---|---|---|---|
| `rep` | 200 random rows (seed 20260822) | 2,267 | 9,825 | seconds per optimizer step |
| `edge` | the 200 longest rows | 10,172 | 12,981 | peak reserved VRAM, worst-case s/step |

`rep`'s mean is 2,790.9 tokens against the corpus's 2,791.6, so its representativeness is
measured rather than asserted.

### The two GPU numbers, measured

Both probes ran on **`ml.g6e.8xlarge`** — not the intended `2xlarge`. `ml.g6e.2xlarge` and
`ml.g6e.4xlarge` both sat on `Training job waiting for capacity`, so three identical payloads
were launched across three sizes and a guard (`race_guard.py`) stopped the two that lost. Every
single-GPU `g6e` size is the same L40S 48 GiB, so the VRAM ceiling transfers exactly; the losers
were Stopped out of `Pending` and billed ≈ 0. The guard is the point — racing pools without one
is just paying twice — and it holds until a second `describe` call confirms only one job is
alive, because "I stopped it" is a belief until then.

Which pool had capacity is itself a cost fact, because all three sizes carry the *same one GPU*
(`aws pricing`, `USE1-Train`, 2026-08-22):

| size | GPUs | $/hr | capacity, 2026-08-22 |
|---|---|---|---|
| ml.g6e.2xlarge | 1 × L40S | 2.80 | none |
| ml.g6e.4xlarge | 1 × L40S | 3.76 | none |
| **ml.g6e.8xlarge** | 1 × L40S | **5.66** | **available** |

The run has to book the pool that exists, at 2.02× the price of the one that does not.

| | `rep` (random 200) | `edge` (longest 200) |
|---|---|---|
| job | `arc2v2-probe-rep-0822d` | `arc2v2-probe-edge-0822c` |
| tokens per row consumed | 2,698.6 | 10,694.2 |
| **s/step, p50 steady** | **7.283** | **30.017** |
| s/step, mean / p90 / first | 7.249 / 9.321 / 8.462 | 30.252 / 32.366 / 33.575 |
| **peak reserved** | **7.72 GiB = 17.4%** | **8.53 GiB = 19.2%** |
| `rows_dropped_overlong` | `{train: 0, val: 0}` | `{train: 0, val: 0}` |
| billable | 451 s ($0.71) | 923 s ($1.45) |

Three things fall out of this, and only one of them was the question asked:

**`max_length 14336` was never the risk.** At the corpus's longest rows the run reserves 19.2%
of a 44.4 GiB device, nowhere near the 0.95 that would have forced a shorter window or a bigger
instance. The 17.4% → 19.2% spread across a 4× difference in sequence length also says the
footprint is dominated by weights, optimizer state and the fused-CE path rather than by
activations, which is why the edge case is not a cliff.

**`--drop_overlong true` dropped nothing, in the job's own metrics, twice.** The CPU census
predicted this; the authority confirms it. Per the plan this was a stop-and-look if non-zero, and
it is not.

**The plan's own wall-clock formula was wrong by 8×.** It reads
`wall = steps × s/step × grad_accum`, but `StepTimerCallback` fires `on_step_end` — once per
*optimizer* step, after all gradient-accumulation micro-batches — and the numbers agree
(`train_runtime` 144.99 s ÷ 20 steps = 7.25 s/step while consuming 160 rows at
`grad_accum` 8). Multiplying by `grad_accum` a second time turns the real 15.8 h into 126.6 h
and rejects a launch that fits. A formula in a plan is not evidence; the unit the code actually
records is.

### Sizing the run off two points instead of one

`rep`'s own mean is the obvious estimator, and it is biased: the 160 rows twenty steps touched
averaged 2,698.6 tokens against the corpus's 2,791.6, 3.3% light. So `size_full_run.py` fits an
affine model over (tokens per step, seconds per step) through both probes, whose prediction
depends only on the corpus's *total* token count — known exactly from the CPU census —
and reports both estimators so their disagreement is visible rather than hidden:

| estimator | full run |
|---|---|
| two-point fit | **15.82 h** |
| `rep` mean only | 15.25 h |
| every row at `edge` length (worst case) | 63.66 h |

They disagree by **3.69%**, which is the gate: above 10% the fit would be extrapolating and the
spread, not the point estimate, would be the answer. The fitted intercept is **negative**
(−0.515 s), which is a measurement rather than a defect — a chord through two points must dip
below zero at zero tokens when the true curve is convex, and attention is quadratic in length.
It also means the chord slightly over-charges the range it interpolates, which is the safe
direction for a runtime limit.

Sized off the slower estimator (7.517 s/step × 7,575 steps):

| parameter | value | why |
|---|---|---|
| steps | 7,575 | `ceil(20,200 × 3 / 8)` |
| `MaxRuntimeInSeconds` | 82,800 (23 h) | hard kill; writes nothing, so it sits above the budget |
| `max_train_seconds` | 73,800 (20.5 h) | 1.30× the estimate; stops gracefully and *saves* |
| `save_steps` | 500 | every 62.6 min, 15 checkpoints — a kill costs ≤ 1 h of GPU |
| expected cost | **$91.41** | at $5.66/h |
| if it runs to the budget stop | $117.92 | the 30% margin, priced |
| if it runs to `MaxRuntime` | $130.18 | the ceiling, priced |
| probes already spent | $2.16 | both, including the two Stopped losers |

All inside the $300 the user set. On `ml.g6e.2xlarge` the same work would be $45.22, so if that
pool returns before launch it halves the bill for an identical GPU.

The one lever left unmeasured: at 17–19% of the device there is room for
`per_device_batch_size > 1`, but batch > 1 pads every row to the longest in its batch and this
corpus spans 178..12,981 tokens, so the padding waste could eat the gain outright. `bs=1` wastes
nothing and is what the plan fixed, so this run does not move it. It is a cheap probe
(~$0.71) if a shorter wall clock is ever worth more than one variable held still.

<!-- /PREFLIGHT-MEASUREMENT-SECTION -->
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
5. **An estimate with a default is an estimate nobody has to supply.** `--sec-per-it` defaulted
   to 27.0 — a number from a different instance, a different model and a quarter of the sequence
   length — so the gate could pass on arithmetic none of whose inputs described the job. Measure
   the input on the target instance first; a flag whose default is a plausible lie is worse than
   a required argument.
6. **A guard that cannot run must not report clean.** The same check was wrapped in
   `if rows and sec_per_it and max_runtime:`, so a missing measurement skipped it silently and
   the script still printed `PASS — safe to launch`. Absent inputs are now FAILs that name the
   probe that produces them. "Not checked" and "checked and fine" must never print the same
   word.
7. **Run the gate on the first real payload, not only on fixtures.** Every unit test of the
   preflight passed while it mis-sized the very payload its own usage line tells you to build:
   `--max_steps` was a flag the arithmetic never read, so a 20-step probe was reported as 75
   steps. Fixtures test the cases someone thought of; the first real payload tests the ones
   they did not.
8. **One dataset cannot measure two numbers.** Seconds per step is an average over the length
   distribution; peak VRAM is set by the longest row. A single 160-row probe answers the first
   and quietly guesses at the second, so the sample is cut twice — random for cost, longest
   rows for the ceiling — and each number is reported against the sample that can support it.
9. **A formula in a plan is not evidence; the unit the code records is.** The plan's
   `wall = steps × s/step × grad_accum` double-counts, because `StepTimerCallback` fires once
   per optimizer step with the micro-batches already inside it. Followed literally it turns a
   15.8-hour run into 126.6 hours and refuses a launch that fits — the same class of arithmetic
   error as v1's, in the opposite direction, and just as expensive.
10. **Two estimators, reported together, are a gate; one is a point estimate.** The
    representative probe's own mean is 3.3% light because the 160 rows it drew were shorter than
    the corpus average. A two-point fit anchored on the exact corpus token total disagrees with
    it by 3.69% — small enough to size a run, and *visible*, which a single number never is.
    Above 10% the disagreement would have been the finding.
11. **Racing capacity pools without a guard is just paying twice.** Two of three `g6e` sizes had
    no capacity, so three identical payloads went out and a guard stopped the losers out of
    `Pending` for ≈ $0. It keeps polling until exactly one job is alive, because a `stop` call
    that returned is not a job that stopped.
