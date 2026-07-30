# v2 Distillation Augmentation Pipeline

Expands 849 sandbox-verified ARC (prompt, code) triplets into a 21k-row
**zero-noise** training set for a small code-generating student model.

## Design

Each source triplet is a prompt (ARC train pairs rendered as space-separated
digit grids) plus a Python `transform(grid)` verified to reproduce every
train pair exactly. We augment with a label-preserving transform group **D**:

| Family | Elements | Per task |
|---|---|---|
| identity (re-verified original) | `orig` | 1 |
| geometric | rot90 / rot180 / rot270 / flip-h / flip-v | 5 |
| color perms | 2× perm of colors 1–9 (0 fixed), 2× perm of all 10 | 4 |
| compositions | each geometric × fresh color perm (2× perm9, 1× perm10) | 15 |

24 augmented variants per task (deterministic per-task RNG seeded from the
task_id; deduped by transform signature).

## The wrapper math

If `g` transforms grids (geometry then color remap) and `f` is the verified
original solution with `f(x) = y` on all train pairs, then

```
transform'(z) = g(f(g⁻¹(z)))
```

satisfies `transform'(g(x)) = g(f(x)) = g(y)` — correct on the transformed
pairs **by construction**. Concretely, the emitted code renames the original
function to `_transform_base` and appends a new `transform` that applies
`inv_perm` lookup + inverse `np.rot90`/`np.flip`, calls the base, then
re-applies the forward geometry and `perm` lookup. The wrapper text is part
of the training target: the student learns to emit it.

## Zero-noise guarantee

Correctness is never assumed, only proven: every candidate (wrapped code,
transformed pairs) is executed in the sandbox (`verify_sandbox.py`, ported
safe-exec: restricted builtins, import whitelist, SIGALRM timeout, exact
cell-by-cell compare) and emitted **only if all pairs reproduce exactly**.
Every row in `out/augmented.jsonl` carries `verified: true` earned by
execution, not by proof-on-paper. A negative control (deliberately corrupted
wrapper) is rejected by the same path.

## Prompt discipline (train == inference format)

Augmented prompts are produced by surgically replacing only the
`(HxW):\n<grid>` blocks inside the ORIGINAL prompt string; every other byte
(header, "Training pair N:" labels, trailer) is preserved. Verified: the
grid-stripped skeleton of every augmented prompt is byte-identical to its
source. The source file contains two header variants (677 + 172 rows); both
are preserved as-is.

## Splits (no leakage)

`make_splits.py` holds out 40 SOURCE task_ids entirely (seed 20260730):
all 25 variants of a held-out task go to val; no variant of a train task
ever appears in val. Outputs TRL messages format (`train.jsonl`,
`val.jsonl`) plus raw copies (`train_raw.jsonl`, `val_raw.jsonl`).

## Stats (run of 2026-07-30)

| Metric | Value |
|---|---|
| Source rows | 849 |
| Emitted (all sandbox-verified) | 21,225 (849 × 25) |
| Rejected (verify-fail) | 0 (0.0%) |
| Train rows / tasks | 20,225 / 809 |
| Val rows / tasks | 1,000 / 40 |
| Median prompt+code tokens (chars/4) | 1,134 (p90 2,114, max 5,656) |
| Full-run wall time (8 workers) | ~108 s |

Per-transform counts live in `out/augment_stats.json`.

## Size-generic note

Nothing in the pipeline assumes grid sizes: rotations of non-square grids
change (HxW) headers, and the re-renderer recomputes them from the actual
transformed grid. Works for any 1×1–30×30 ARC grid and any number of train
pairs.

## Eval: verifiable, not judged

The student is scored by **executing** what it writes. A generation counts as
solved only when its `transform` reproduces every training pair in its prompt,
cell for cell, in the same sandbox that gated the training data. No LLM judge,
no ROUGE, no partial credit for code that merely looks plausible.

Generation and scoring are separate processes on purpose:

| Stage | Where | Input → output |
|---|---|---|
| `generate_student.py` | GPU (SageMaker) | `val_raw.jsonl` → `generations.jsonl` |
| `eval_student.py score` | CPU (anywhere) | `generations.jsonl` → `eval_report.json` |

The split means a scoring bug never costs GPU time to re-fix, and the scorer is
testable with no torch installed. Scoring reconstructs ground-truth pairs from
the prompt text itself, so a generations file carries only `task_id`, `variant`,
and `generation`. Decoding is greedy (`--temperature 0`) so the gate is a number,
not a distribution.

**Format discipline.** Training targets are bare unfenced code, and
`generate_student.py` renders eval prompts through the same
`apply_chat_template` call training used. A mismatch here tanks the solve rate
while the model is fine, so it is stated in code and covered by tests.

### Trusting the scorer in both directions

An eval that can be fooled manufactures confidence, so it is attacked from both
sides before any student number is believed:

```
$ python eval_student.py self-test --val out/val_raw.jsonl --sample 80
oracle: solved 80/80 (solve_rate 1.000, format 1.000)
PASS — scorer credits every verified solution
```

The oracle direction feeds the *verified ground-truth* code back in as if the
model had emitted it; anything below 1.000 means the scorer rejects known-correct
solutions and would understate the model. The adversarial direction lives in
`tests/test_eval_student.py` (23 tests): a wrong answer, a partially-correct
answer, a hardcoded output that memorizes pair 1, crashing code, an infinite
loop, a filesystem-escape attempt, a generation for an unknown task, and a
sibling *variant* of the right task must all score 0 — that last one matters
because variants share a `task_id` but need different code, so matching on
`task_id` alone would be a silent leak. Prompt parsing is verified against all
1,000 val rows (0 failures) and rejects any grid whose contents contradict its
declared `HxW`.

**Gate.** Relative, not absolute: `student_solve_rate >= 0.80 × teacher_solve_rate`
on the same rows, because absolute ARC-AGI-2 solve rates are low for a 1.7B
student. With no teacher baseline the gate reports `NO_TEACHER_BASELINE` and
`passed: null` rather than claiming a pass. A 0% teacher makes a 0% student pass
arithmetically — that is reported honestly instead of dressed up as quality.

## Files

- `augment.py` — augmentation engine (multiprocessing; `--limit` for smoke
  tests, `--n-variants`, `--workers`)
- `verify_sandbox.py` — sandboxed exec + exact grid compare (also reusable
  for eval)
- `make_splits.py` — deterministic task-level split, TRL + raw formats
- `generate_student.py` — GPU-side generation (greedy; writes incrementally so
  an interrupted run stays scorable)
- `eval_student.py` — `score` (execute + gate) and `self-test` (oracle check)
- `out/` — `augmented.jsonl`, `train[.raw].jsonl`, `val[.raw].jsonl`,
  `augment_stats.json`, `split_stats.json`
