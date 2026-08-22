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

## What zero-noise does **not** guarantee (the held-out gate)

`verified: true` means the code reproduces the train pairs **that were in its own
prompt**. The author of that code — a teacher LLM — saw those pairs. So the verdict
cannot fail for the reason anyone cares about, and the failure it cannot see is the
common one: a rule that is pinned by 2–4 examples and wrong on a fifth.

The augmentation makes this worse rather than better, and by construction. Because
`transform'(z) = g(f(g⁻¹(z)))` is correct *whenever `f` is*, a wrong `f` is replicated
faithfully across all 25 variants, every one of them passes the sandbox, and
`rejection_rate` stays `0.0%`. **One wrong source solver is 25 wrong rows that no
downstream number distinguishes from good ones.** Zero-noise is a statement about
execution, not about rules.

Measured, non-circularly, on the real ARC training corpus — `distill_results.json`
records a `test_score` per task that no gate in this pipeline had ever read:

| Population | Also correct on the unseen test pair | Wilson 95% |
|---|---|---|
| all shown-pair-verified solvers | **677 / 742 = 91.2%** | [89.0, 93.1] |
| clean (solved on the first sample) | 439 / 463 = 94.8% | [92.4, 96.5] |
| repaired (told which pair mismatched, ≥2 rounds) | 238 / 279 = 85.3% | [80.7, 89.0] |

and monotone in repair effort: round 1 → 94.8%, 2 → 90.2%, 3 → 78.7%, 7 → 66.7%. So
the tax is **8.8% overall / 14.7% on repaired solvers**, and "verified after being told
which example failed" is a materially weaker claim than "verified".

`build_heldout_source.py` closes it at the source: it executes each solver against the
ARC **test** pairs — which its author never saw — and drops the ones that fail.

- Every test pair is checked, not the first: 69 of the 1,000 training tasks have more
  than one, and a rule that is nearly right is exactly the one that handles one unseen
  input and not the other.
- A rejected row keeps its `code` in `<out>.rejected.jsonl`. That file is the evidence
  about the teacher; deleting it would make the gate unfalsifiable.
- **Repair rounds are a tag, never a filter.** After the gate, a repaired solver is
  held-out-correct by the same definition as a clean one, so excluding it would discard
  the hardest tasks in exchange for no measured gain. The count travels as
  `repair_rounds` (missing → `"unknown"`, never `0`, which would flatten the
  dose-response above into the clean bucket).
- **`task_id` is not a key for that tag.** A corpus is assembled over several
  distillation passes and a later pass can replace a task's solver while keeping its id,
  which leaves the recorded round count describing a program that is no longer there.
  Measured: only **676 of 848** rows carry the code their provenance entry describes, and
  among the entries recording `rounds_used: 10` (the loop cap) just **6 of 154** do.
  Joined on `task_id` alone, those 148 rows reported `10 → 154/154 = 100%` — a wrong join
  sitting in the report as evidence *against* the dose-response two paragraphs above. The
  tag is therefore only used when the entry's `code` **is** the row's code; otherwise it
  reads `"superseded"`, kept distinct from `"unknown"` because they call for different
  work, and the report's `provenance` block counts all three so a table whose largest
  bucket is `superseded` cannot be read as a measurement.
- An empty result exits non-zero, and a corpus whose `task_id`s do not appear in the ARC
  files is refused rather than reported as "0 gated of 0 matched".
- The gate must be able to say no, so `gate_self_check()` runs three probes before any
  data does: accept a correct solver, reject one that differs by a **single character**,
  reject an infinite loop. A permissive verifier — a SIGALRM that never fires, a
  comparison that coerces types — reports every solver held-out-correct, which reads
  exactly like a corpus that needed no gating.

`augment.py` then requires the held-out pair to survive its own `g`, and carries it into
every emitted variant, so the pair is genuine ground truth *for that variant's prompt*.
Three statuses are kept distinct because they mean different things: `missing` (the
measurement was never taken — refused by default, `--no-require-heldout` to opt out
explicitly), `gated_out` (the program is wrong — the row is dropped), and
`wrapper_mismatch` (train pairs pass but the transformed held-out pair does not — that is
a **defect in `augment.py`**, not bad data, and it exits 1).

Applied to the existing 849-row corpus: **848 pass (99.88%)**; the one failure
(`4e45f183`) raises on the test input. The corpus was already clean, so this gate is a
guard rail for data yet to be bought, not a repair of what is on disk.

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

**Which 40, and why not just `rng.sample`.** The holdout takes one task from each of
40 equal-width prompt-length strata rather than a uniform sample of 40 of 848. A
uniform sample is unbiased *in expectation*, and a pinned seed does not draw an
expectation — it draws once and keeps it. Measured with the student's own tokenizer,
seed 20260730's uniform draw gave a val set whose task-median was **3,334 tokens
against the corpus's 2,340 — 47% longer**, outside the 90% band [1,974, 2,827] of
20,000 resamples, two-sided **p = 0.0009**. Nothing was wrong with the sampler.

Prompt length in this corpus *is* grid size, so that draw quietly made every val
number a measurement of the corpus's harder end: `eval_loss` not comparable to train
loss, and both solve rates reported over systematically bigger grids than the model
was trained on. Stratified, the same seed gives a val task-median of **2,423 against
2,340 (p = 0.735)**, and `split_stats.json` now records the median prompt length of
each side so the absence of the bias is visible rather than asserted. `--seed` still
varies which task comes out of each stratum, so reruns stay reproducible and
variable — just never length-extreme.

The property under test is structural, in `tests/test_make_splits.py`
(7 tests): exactly one task per stratum, which is what makes an extreme draw
unreachable. The negative control matters more than the property — it searches seeds
for a *uniform* draw that IS extreme on the same corpus and then shows the stratified
selection cannot produce one at any of those seeds. Without it, a test that only
checked "the median is close" would pass for the uniform sampler most of the time,
which is exactly how the defect survived.

## Stats (corpus of 2026-08-22)

| Metric | Value |
|---|---|
| Source rows | 848 (849 gated, 1 dropped: `4e45f183` fails its held-out pair) |
| Emitted (all sandbox-verified) | 21,200 (848 × 25) |
| Rejected (verify-fail) | 0 (0.0%) |
| Train rows / tasks | 20,200 / 808 |
| Val rows / tasks | 1,000 / 40 |
| Median prompt+code tokens (student tokenizer) | 2,319 (p90 5,214, p99 9,585, max 12,981) |
| Full-run wall time (8 workers) | ~108 s |

Per-transform counts live in `out/augment_stats.json`; the token census and the
per-task length derivation live in `~/Documents/llmops-evidence/arc2-v2/`.

Those token counts are measured with the tokenizer and chat template of
**`Qwen/Qwen3-4B-Thinking-2507`** — the model that will actually train — pulled from
its in-account mirror, over all 21,200 rows. Both halves of that matter. An earlier
version of this table used the `chars / 4` rule of thumb and understated every figure
by 2.0–2.5× (it reported median 1,134 / p90 2,114 / max 5,656); the rule fails badly
on this specific domain, where ARC prompts are space-separated single digits and one
token covers about two characters rather than four. The version after that fixed the
estimate but measured with `Qwen/Qwen3-1.7B`, a same-family stand-in for a model that
was no longer the student — and a `max_length` is sized against the tokenizer that
will do the counting, not a relative of it. Anything sized from the estimate — a
`max_length`, a `max_new_tokens`, a cost projection — would have been wrong by
more than a factor of two, so the measurement is worth its one-off cost.

### The `max_length` cliff (read this before setting `--max-length`)

At the `max_length 4096` used by the 2026-07-31 training run, `--drop-overlong`
discards **3,625 train rows (17.95%)** and **150 val rows (15.0%)**. Those rows are
not a random sample, and the reason is structural: a row's length is set almost
entirely by its grid size, and all 25 variants of a source task share that size
(the augmentation group permutes colors and applies a geometry, neither of which
changes the cell count — measured, every variant of a task tokenizes its *prompt* to
the identical length). So dropping is **nearly all-or-nothing per source task**, and
it removes the largest grids first:

| Split | Rows dropped | Source tasks *entirely* removed |
|---|---|---|
| train | 3,625 / 20,200 (17.95%) | **130 / 808 (16.09%)** |
| val | 150 / 1,000 (15.0%) | **6 / 40 (15.0%)** |

"Nearly", because the row is prompt **plus code** and the code is per-variant: the
wrapper encodes that variant's colour permutation and geometry, so it varies by up to
~150 tokens where the prompt does not vary at all. At 4096, **19 of 808 train tasks
straddle the window** — some variants in, some out. `0a938d79` is the clean example:
prompt 3,301 tokens for all 25 variants, code 750→897, row totals 4,065→4,212 across a
window at 4,096. The earlier claim that the variants "share that size" was true of the
prompt and false of the row, which is the kind of gap that makes a task look retained
while a third of its variants are gone.

Two consequences worth stating plainly rather than discovering later:

- A `eval_loss` or `solve_rate` from that configuration covers **34 val tasks, not
  40**, and the 6 missing ones are the hardest-to-render (largest) tasks in the
  held-out set. Reported without this note, a deliberate configuration choice reads
  as a model deficiency.
- The student is never shown a large grid at all, so it cannot be expected to
  generalize to one. Raising `max_length` to 8192 would recover most of the gap
  (p90 is 5,214, p99 9,585 — 394 train rows and 15 tasks still lost, 1.95%) at
  roughly 2× attention memory per sample — the compensating knob is
  `per_device_batch_size` with `gradient_accumulation` raised to hold the effective
  batch size fixed.

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
solutions and would understate the model. Measured over **all 1,000 val rows**:
1000/1000, `format 1.000`.

An oracle pass only proves the scorer does not *reject* correct code — not that it
*discriminates*. The adversarial direction lives in `tests/test_eval_student.py`
(40 tests): a wrong answer, a partially-correct answer, a hardcoded output that
memorizes pair 1, crashing code, code that does not compile, an infinite loop, a
filesystem-escape attempt, a generation for an unknown task, and a sibling
*variant* of the right task must all score 0 — that last one matters because
variants share a `task_id` but need different code, so matching on `task_id` alone
would be a silent leak. Run as a spread over 200 real val rows, the separation is:

| Control | solve_rate | format_valid_rate |
|---|---|---|
| oracle (verified code) | 1.000 | 1.000 |
| identity `return grid` (valid, wrong) | 0.000 | 1.000 |
| verified code, wrong task | 0.005 | 1.000 |
| code that does not compile | 0.000 | 0.000 |
| prose, no code block | 0.000 | 0.000 |

Prompt parsing is verified against all 1,000 val rows (0 failures) and rejects any
grid whose contents contradict its declared `HxW`.

The single non-zero off-diagonal is real and expected: 1 of 200 ARC tasks is solved
by another task's verified program, because some ARC transformations genuinely
coincide on small grids. It is a property of the task set, not a scorer defect —
worth stating so a future 0.005 floor is not mistaken for leakage.

### Gate — and what the baseline can honestly mean

The gate is `student_solve_rate >= 0.80 × teacher_solve_rate` on the same rows,
because absolute ARC-AGI-2 solve rates are low for a 1.7B student.

**The trap in this particular val set:** all 1,000 rows exist *only because* a
teacher solution was found and sandbox-verified for their source task — 40 of 40
held-out tasks have `verified: true`. So a teacher re-measured on these rows
scores ~1.0 **by construction**, and the "relative" gate silently degenerates into
an absolute 80% bar. That is a selection effect in the data, not a property of the
teacher, and a 0.80× label would imply a comparison it isn't making.

A baseline is therefore only informative when it is measured on rows chosen
*independently* of whether a solution was found — the untouched ARC-AGI-2
evaluation split, not this one. The scorer enforces the honesty rather than
trusting the reader to remember it: when a baseline is ≥ 0.99 (or exactly 0), the
report carries a `baseline_caveat` spelling out what the gate is actually testing.
With no baseline at all it reports `NO_TEACHER_BASELINE` / `passed: null` rather
than claiming a pass.

The absolute numbers to read alongside the gate are `solve_rate` (executable
correctness) and `format_valid_rate` (did it emit a `transform` that **compiles**)
— for a 1.7B student on ARC-AGI-2, the second is the more informative early signal.

`format_valid_rate` counts only code that parses, and `n_unparseable_code` reports
the rest separately from `no_transform_emitted`. The distinction is not cosmetic:
the two failures say different things about the model (one tried and ran out of
tokens, the other never wrote a `transform`) and only the first is fixed by raising
`--max-new-tokens`. This was a real defect, found by running the negative controls
above rather than by any test: `extract_code` regex-matches a `def transform(` line,
so a body cut off mid-expression carried the signature and scored `format_valid`
**1.000** over 200 rows. A model emitting 200 unparseable stubs would have read as
perfectly well-formed — through both the `format_validity: 0.95` pipeline gate and
the verdict `compute_lift` falls back to when both solve rates are 0. Code that
compiles and then crashes (`NameError`) stays format-valid: that is a wrong program,
not a malformed one.

### Lift: the comparison that actually answers the question

Pass `--base-report` (the **same** model scored *before* fine-tuning) and the
report gains a `lift` block. This is the measurement that answers "did the
distillation do anything", and unlike the teacher baseline it **cannot be inflated
by how the val set was selected** — the selection effect that hands the teacher
~1.0 applies identically to both sides, so it cancels. Same prompts, same greedy
decoding, same executable scoring; only the weights differ.

```
$ python eval_student.py score --generations gen.jsonl --val out/val_raw.jsonl \
    --out eval_report.json --base-report base_report.json --teacher-report teacher.json
scored 30/30 generations
  format-valid : 30 (100.0%)
  solved       : 20 (66.7%)
  gate         : FAILED
  CAVEAT       : teacher solve rate is 1.000; on a val set built from verified
                 solutions this is expected by construction, so the gate is
                 really an absolute 80.0% bar, not a 80% comparison
  vs base      : 0.0% -> 66.7% (+66.7%)
  lift verdict : fine-tuning improved executable correctness
```

Three honesty cases are pinned by tests: a **regression** is named as one rather
than reported as a quiet negative gain; a zero base reports `relative_gain: null`
instead of dividing by zero; and when both solve rates are 0 — entirely possible
for a 1.7B model here — the verdict redirects to `format_valid_rate` rather than
letting a 0.0 gain imply the question was settled.

**Both runs must be prompted identically or the lift measures the prompt.** Qwen3
is a thinking model, and the un-fine-tuned base will open with a long `<think>`
chain — often spending the entire token budget there and emitting no `transform` at
all. Scored naively that reads as "the base model cannot write code" when the real
cause is a decoding ceiling, which would inflate the apparent lift. Two mechanisms
keep that visible instead of invisible:

- `--thinking {auto,on,off}` passes Qwen3's `enable_thinking` template switch, so
  the fine-tuned and base runs can be rendered the same way. `auto` passes nothing
  and leaves the template's own default alone. The chosen value is recorded in
  `<out>.done` alongside `model_dir`, `max_new_tokens`, and `temperature` — the
  settings a comparison has to match.

  What the flag actually does, checked against the live Qwen3-1.7B template rather
  than assumed: it reaches the template as a Jinja variable, and the template acts
  on it **only to suppress** thinking —

  ```jinja
  {%- if enable_thinking is defined and enable_thinking is false %}
      {{- '<think>\n\n</think>\n\n' }}
  ```

  So `off` prefills an empty think block, while `on` and `auto` render byte-identically;
  `on` is a no-op on this model and the generator prints that rather than implying it
  forced something. Worse, an unsupported flag **raises nothing** — `apply_chat_template`
  forwards unknown kwargs into the Jinja context, so a template with no
  `enable_thinking` in it accepts the flag in total silence and thinking is never
  suppressed. `check_thinking_effect` therefore asks the template instead of trusting
  it: it renders a probe both ways before the weights load, and a `--thinking off` that
  changes nothing is fatal, because a lift comparison built on it would be measuring
  two identically prompted runs.
- Every generation carries `truncated` and `n_new_tokens`, and the scorer counts how
  many format failures hit the ceiling:

```
  format-valid : 5 (25.0%)
  TRUNCATED    : 8 of the 15 format failures ran out of generation tokens
                 mid-answer, so format_valid_rate (0.250) partly measures the
                 token budget, not the model; raise --max-new-tokens before
                 reading it as ability
```

The caveat fires only for failures that actually ran out of tokens — a model that
simply refused to write code gets no budget excuse, and truncated-but-parseable code
is still scored on its merits.

### Dry-running the generator before it costs GPU time

`generate_student.py` only ever runs after a multi-hour training job, on a machine the
unit tests can't reach — so a crash in it is discovered at the most expensive possible
moment. It therefore takes `--device cpu` and can be exercised against a tiny randomly
initialised Qwen3 carrying the **real** Qwen3-1.7B tokenizer: real chat template, real
`generate`, real batching, meaningless numbers. Doing that once (2026-07-31) found
three defects that 19 passing unit tests had not:

| Found by executing, missed by the stubs | Why the stubs missed it |
|---|---|
| `device_map="auto"` makes `accelerate` a hard requirement — `ValueError` at load, and no generation-side requirements file installs it | the stub model's `from_pretrained` ignored its kwargs |
| `torch_dtype=` is renamed `dtype=` in transformers 5; the DLC pins only `transformers>=4.52`, so the correct name is a runtime fact | no real `from_pretrained` to warn |
| with `--batch-size > 1`, `generate` returns a **padded rectangle**, so `len(new_ids)` is the batch maximum — a row that stopped after 3 tokens reported `truncated=True` at a ceiling of 10 | the stub returned fixed-length rows, so no batch was ever ragged |

The third one mattered most: it fed rows that finished cleanly into the truncation
caveat above, excusing format failures the token budget did not cause — the caveat
would have argued against its own evidence. `trim_new_tokens` now measures each row
from its stop token (or its trailing pad run) and is pinned by seven tests, each
confirmed by mutation to fail when the behaviour it describes is removed.

```
$ python generate_student.py --model-dir /tmp/tinyqwen3 --val val_raw.jsonl \
    --out gen.jsonl --max-new-tokens 8 --batch-size 3 --device cpu
[gen] device=cpu dtype=torch.float32 torch=2.13.0 transformers=5.14.1
[gen] stop ids [151645], pad 151643
```

The printed device/dtype/version line is part of the point: a lift comparison whose two
halves loaded under different dtypes is not a comparison, and now that is visible in the
log rather than inferred.

## Files

- `augment.py` — augmentation engine (multiprocessing; `--limit` for smoke
  tests, `--n-variants`, `--workers`). The corpus is an argument, not a built-in
  location: `--source <triplets.jsonl>` (or `$V2_SOURCE_JSONL`) is **required**
  and names the distill output holding the sandbox-verified `(task_id, prompt,
  code)` rows — there is no default, because augmenting the wrong corpus yields a
  zero-noise training set for the wrong task and nothing downstream would notice.
  `--arc-dir` (or `$V2_ARC_TRAINING_DIR`, default `/tmp/arc/data/training`) is an
  optional speedup only; without it the train pairs are parsed back out of the
  prompt text.
- `build_heldout_source.py` — the held-out gate: attaches each task's ARC **test**
  pairs to its solver, executes the solver against them, and writes only the survivors
  (`--source`, `--challenges`, `--solutions`, `--out`; `--provenance` for repair
  rounds). Rejects go to `<out>.rejected.jsonl` with their code, the rates to
  `<out>.report.json`. Exits non-zero on an empty result, an empty source, or a corpus
  whose task_ids the ARC files do not describe.
- `verify_sandbox.py` — sandboxed exec + exact grid compare (also reusable
  for eval)
- `make_splits.py` — deterministic task-level split, TRL + raw formats; carries
  `heldout_pairs` through to `val_raw.jsonl` verbatim
- `generate_student.py` — GPU-side generation (greedy; writes incrementally so
  an interrupted run stays scorable)
- `eval_student.py` — `score` (execute + gate) and `self-test` (oracle check)
- `out/` — `augmented.jsonl`, `train[.raw].jsonl`, `val[.raw].jsonl`,
  `augment_stats.json`, `split_stats.json`
