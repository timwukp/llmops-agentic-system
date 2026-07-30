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

## Files

- `augment.py` — augmentation engine (multiprocessing; `--limit` for smoke
  tests, `--n-variants`, `--workers`)
- `verify_sandbox.py` — sandboxed exec + exact grid compare (also reusable
  for eval)
- `make_splits.py` — deterministic task-level split, TRL + raw formats
- `out/` — `augmented.jsonl`, `train[.raw].jsonl`, `val[.raw].jsonl`,
  `augment_stats.json`, `split_stats.json`
