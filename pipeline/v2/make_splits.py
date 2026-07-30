"""Deterministic train/val split of the augmented set — split by SOURCE task.

Leakage rule: 40 source task_ids are held out ENTIRELY. Every variant of a
held-out task (original + all augmentations) goes to val; no variant of any
training task ever appears in val. Augmentations of a task are near-
duplicates of each other, so splitting at the row level would leak.

Outputs (pipeline/v2/out/):
  train.jsonl / val.jsonl              — TRL messages format:
      {"messages": [{"role": "user", "content": prompt},
                    {"role": "assistant", "content": code}]}
  train_raw.jsonl / val_raw.jsonl      — raw rows (task_id, variant, prompt,
                                          code, n_train_pairs, verified)
  split_stats.json                     — sizes + held-out task ids

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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-holdout", type=int, default=40)
    ap.add_argument("--seed", type=int, default=20260730)
    args = ap.parse_args()

    rows = [json.loads(line) for line in open(AUGMENTED)]
    task_ids = sorted({r["task_id"] for r in rows})
    rng = random.Random(args.seed)
    holdout = set(rng.sample(task_ids, args.n_holdout))

    train = [r for r in rows if r["task_id"] not in holdout]
    val = [r for r in rows if r["task_id"] in holdout]
    assert len(train) + len(val) == len(rows)
    assert not ({r["task_id"] for r in train} & {r["task_id"] for r in val}), \
        "leakage: task_id present in both splits"

    write_jsonl(os.path.join(OUT_DIR, "train_raw.jsonl"), train)
    write_jsonl(os.path.join(OUT_DIR, "val_raw.jsonl"), val)
    write_jsonl(os.path.join(OUT_DIR, "train.jsonl"),
                [to_messages(r) for r in train])
    write_jsonl(os.path.join(OUT_DIR, "val.jsonl"),
                [to_messages(r) for r in val])

    stats = {
        "seed": args.seed,
        "total_rows": len(rows),
        "source_tasks": len(task_ids),
        "holdout_tasks": args.n_holdout,
        "train_rows": len(train),
        "val_rows": len(val),
        "train_tasks": len({r["task_id"] for r in train}),
        "val_tasks": len({r["task_id"] for r in val}),
        "holdout_task_ids": sorted(holdout),
    }
    with open(os.path.join(OUT_DIR, "split_stats.json"), "w") as f:
        json.dump(stats, f, indent=2)
    print(json.dumps({k: v for k, v in stats.items()
                      if k != "holdout_task_ids"}, indent=2))


if __name__ == "__main__":
    main()
