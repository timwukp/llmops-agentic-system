"""Deterministic train/val split of the augmented set — split by SOURCE task.

Leakage rule: 40 source task_ids are held out ENTIRELY. Every variant of a
held-out task (original + all augmentations) goes to val; no variant of any
training task ever appears in val. Augmentations of a task are near-
duplicates of each other, so splitting at the row level would leak.

Outputs (pipeline/v2/out/):
  train.jsonl / val.jsonl              — TRL messages format:
      {"messages": [{"role": "user", "content": prompt},
                    {"role": "assistant", "content": code}]}
  train_raw.jsonl / val_raw.jsonl      — raw rows verbatim (task_id, variant,
                                          prompt, code, n_train_pairs, verified,
                                          heldout_pairs, heldout_ok,
                                          repair_rounds)
  split_stats.json                     — sizes + held-out task ids

`heldout_pairs` travels into val_raw.jsonl because eval_student.py scores one
row at a time and cannot get back to the source task: without it, a student is
graded only on the pairs its own prompt showed it, which is the same tautology
the teacher gate had. Coverage must be all-or-nothing — a corpus where some
val rows carry the pairs and some do not gives the held-out rate a different
denominator from solve_rate, and the two get compared anyway.

Usage: python3 make_splits.py [--n-holdout 40] [--seed 20260730]
"""
from __future__ import annotations

import argparse
import json
import os
import random

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
AUGMENTED = os.path.join(OUT_DIR, "augmented.jsonl")


def to_messages(row: dict) -> dict:
    return {"messages": [
        {"role": "user", "content": row["prompt"]},
        {"role": "assistant", "content": row["code"]},
    ]}


def write_jsonl(path: str, rows: list[dict]) -> None:
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


MIN_TRAIN_MULTIPLE = 5


def stratified_holdout(rows: list[dict], n_holdout: int, seed: int) -> set[str]:
    """Pick n_holdout source tasks spread evenly across the prompt-length range.

    A uniform `rng.sample` is unbiased in expectation and can still hand back one bad
    draw, and with a pinned seed that draw is permanent. Measured on this corpus with
    the student's own tokenizer: seed 20260730's uniform sample of 40 of 848 tasks had
    a task-median of 3,334 tokens against the corpus's 2,340 -- **47% longer**, outside
    the 90% band [1,974, 2,827] of 20,000 resamples, two-sided p = 0.0009. Nothing was
    wrong with the sampler; it drew a 1-in-1000 sample and pinned it.

    That is not a cosmetic problem, because prompt length here IS grid size: a val split
    of systematically bigger grids makes `eval_loss` not comparable to train loss, and
    makes both solve rates measurements of the harder end of the corpus while reading as
    measurements of the corpus.

    So stratify: order tasks by prompt length, cut into n_holdout equal-width strata, and
    take one task from each. Length is proxied by the prompt's character count -- this
    module has no tokenizer and must not acquire one (it would put a GPU-era dependency
    on a 2-second CPU split), and within this domain the proxy is tight: prompts are
    space-separated single digits, so characters and tokens differ by a near-constant
    factor. The result still depends on `seed` -- which task comes out of each stratum --
    so a rerun is reproducible and a different seed still gives a different split, just
    never a length-extreme one.
    """
    by_task: dict[str, int] = {}
    for r in rows:                      # a task's variants share one prompt length
        by_task.setdefault(r["task_id"], len(r["prompt"]))
    ordered = sorted(by_task, key=lambda t: (by_task[t], t))   # tie-break by id: stable
    rng = random.Random(seed)
    holdout = set()
    n = len(ordered)
    for i in range(n_holdout):
        lo, hi = i * n // n_holdout, (i + 1) * n // n_holdout
        stratum = [t for t in ordered[lo:hi] if t not in holdout]
        if not stratum:                 # only reachable if strata are thinner than 1 task
            raise SystemExit(
                f"stratum {i} of {n_holdout} is empty over {n} tasks -- "
                f"--n-holdout must not exceed the task count")
        holdout.add(rng.choice(stratum))
    assert len(holdout) == n_holdout, (len(holdout), n_holdout)
    return holdout


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-holdout", type=int, default=40)
    ap.add_argument("--seed", type=int, default=20260730)
    args = ap.parse_args()

    rows = [json.loads(line) for line in open(AUGMENTED)]
    task_ids = sorted({r["task_id"] for r in rows})

    # 40 held-out tasks is ~5% of the 849-task corpus this default was chosen
    # for, and 40% of a 100-task one. The failure is silent: the split succeeds,
    # every assertion below passes, and the training set is a fifth of what the
    # run reported buying. Refuse instead, naming the flag that fixes it.
    if args.n_holdout < 1:
        raise SystemExit("--n-holdout must be at least 1")
    if len(task_ids) < MIN_TRAIN_MULTIPLE * args.n_holdout:
        raise SystemExit(
            f"{len(task_ids)} source tasks cannot spare {args.n_holdout} to val: "
            f"holding out more than 1/{MIN_TRAIN_MULTIPLE} of the corpus leaves a "
            f"training set the run did not intend. Lower --n-holdout (max "
            f"{len(task_ids) // MIN_TRAIN_MULTIPLE} here) or augment more sources.")

    holdout = stratified_holdout(rows, args.n_holdout, args.seed)

    train = [r for r in rows if r["task_id"] not in holdout]
    val = [r for r in rows if r["task_id"] in holdout]
    assert len(train) + len(val) == len(rows)
    assert not ({r["task_id"] for r in train} & {r["task_id"] for r in val}), \
        "leakage: task_id present in both splits"

    n_val_heldout = sum(1 for r in val if r.get("heldout_pairs"))
    if 0 < n_val_heldout < len(val):
        raise SystemExit(
            f"held-out coverage is partial: {n_val_heldout}/{len(val)} val rows "
            f"carry heldout_pairs. A mixed corpus makes heldout_solve_rate and "
            f"solve_rate rates over different row sets, which will be compared as "
            f"if they were not. Re-augment from a single gated source.")

    write_jsonl(os.path.join(OUT_DIR, "train_raw.jsonl"), train)
    write_jsonl(os.path.join(OUT_DIR, "val_raw.jsonl"), val)
    write_jsonl(os.path.join(OUT_DIR, "train.jsonl"),
                [to_messages(r) for r in train])
    write_jsonl(os.path.join(OUT_DIR, "val.jsonl"),
                [to_messages(r) for r in val])

    # The bias this split was rebuilt to remove is only absent if someone can see it is
    # absent, so record the length distribution of both sides rather than the seed alone.
    task_len = {}
    for r in rows:
        task_len.setdefault(r["task_id"], len(r["prompt"]))

    def med(vals):
        s = sorted(vals)
        return s[len(s) // 2] if s else None

    stats = {
        "seed": args.seed,
        "holdout_selection": "stratified by prompt length (one task per equal-width stratum)",
        "prompt_chars_median": {
            "corpus": med(list(task_len.values())),
            "train_tasks": med([v for t, v in task_len.items() if t not in holdout]),
            "val_tasks": med([v for t, v in task_len.items() if t in holdout]),
        },
        "total_rows": len(rows),
        "source_tasks": len(task_ids),
        "holdout_tasks": args.n_holdout,
        "train_rows": len(train),
        "val_rows": len(val),
        "train_tasks": len({r["task_id"] for r in train}),
        "val_tasks": len({r["task_id"] for r in val}),
        "val_rows_with_heldout_pairs": n_val_heldout,
        "holdout_task_ids": sorted(holdout),
    }
    if not n_val_heldout:
        stats["heldout_caveat"] = (
            "no val row carries heldout_pairs, so eval_student.py can only score "
            "against the pairs each prompt already shows the model. That rate "
            "measures whether the student writes runnable code, not whether it "
            "found the rule; re-augment from a source built by "
            "build_heldout_source.py to get the second number")
    with open(os.path.join(OUT_DIR, "split_stats.json"), "w") as f:
        json.dump(stats, f, indent=2)
    print(json.dumps({k: v for k, v in stats.items()
                      if k != "holdout_task_ids"}, indent=2))


if __name__ == "__main__":
    main()
