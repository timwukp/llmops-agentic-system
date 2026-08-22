"""Tests for the train/val split — the holdout must not be a length-extreme draw.

`make_splits.py` had no tests at all, and it held out 40 source tasks with a pinned
`random.Random(20260730).sample(...)`. Unbiased in expectation, and that is the trap: one
draw is not an expectation. Measured on the built corpus with the student's own tokenizer,
that seed's 40 tasks had a task-median of 3,334 tokens against the corpus's 2,340 -- 47%
longer, outside the 90% band [1,974, 2,827] of 20,000 resamples, two-sided p = 0.0009. The
sampler was correct and the split was still wrong, permanently, because the seed is pinned.

Prompt length here is grid size, so a longer val split silently makes `eval_loss`
incomparable to train loss and turns both solve rates into measurements of the corpus's
harder end. These tests pin the structural property that rules the bad draw out --
one task per equal-width length stratum -- rather than re-measuring a p-value, which
would make the suite depend on a corpus CI does not have.

Run: .venv/bin/python -m pytest tests/test_make_splits.py -q
"""
from __future__ import annotations

import pathlib
import statistics
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pipeline/v2"))

import make_splits  # noqa: E402


def _corpus(lengths, variants=3):
    """A corpus of tasks whose prompt lengths are `lengths`, each with N variants."""
    rows = []
    for i, n in enumerate(lengths):
        for v in range(variants):
            rows.append({"task_id": f"t{i:04d}", "variant": f"v{v}",
                         "prompt": "x" * n, "code": "pass"})
    return rows


# A heavy right tail, which is what the real corpus has: most tasks small, a few huge.
SKEWED = [200 + int(1.9 ** (i % 20)) for i in range(400)]


def test_the_holdout_has_exactly_one_task_from_each_length_stratum():
    """The property that makes an extreme draw unreachable, asserted directly.

    Not "the median is close" -- that is a statement about one corpus and one seed, and it
    passes for a uniform sampler most of the time, which is precisely the failure mode.
    """
    rows = _corpus(SKEWED)
    n_holdout = 40
    holdout = make_splits.stratified_holdout(rows, n_holdout, seed=1)
    by_task = {}
    for r in rows:
        by_task.setdefault(r["task_id"], len(r["prompt"]))
    ordered = sorted(by_task, key=lambda t: (by_task[t], t))
    n = len(ordered)
    for i in range(n_holdout):
        lo, hi = i * n // n_holdout, (i + 1) * n // n_holdout
        picked = [t for t in ordered[lo:hi] if t in holdout]
        assert len(picked) == 1, f"stratum {i} ({lo}:{hi}) contributed {picked}"


def test_a_uniform_sample_of_the_same_corpus_can_be_extreme_and_this_one_cannot():
    """The negative control. Without it the test above proves only that the code runs.

    Searches seeds for a uniform draw whose median sits far outside the corpus median, then
    shows the stratified selection cannot produce one at ANY of those seeds. If the uniform
    sampler were harmless, the first loop would find nothing and this test would fail.
    """
    import random
    rows = _corpus(SKEWED)
    by_task = {}
    for r in rows:
        by_task.setdefault(r["task_id"], len(r["prompt"]))
    pop = statistics.median(list(by_task.values()))
    tasks = sorted(by_task)

    bad_seeds = []
    for seed in range(200):
        draw = random.Random(seed).sample(tasks, 40)
        if abs(statistics.median([by_task[t] for t in draw]) - pop) > 0.4 * pop:
            bad_seeds.append(seed)
    assert bad_seeds, "no uniform draw was extreme -- this corpus cannot show the defect"

    for seed in bad_seeds:
        strat = make_splits.stratified_holdout(rows, 40, seed=seed)
        got = statistics.median([by_task[t] for t in strat])
        assert abs(got - pop) <= 0.4 * pop, \
            f"seed {seed}: stratified median {got} is as extreme as the uniform draw"


def test_the_same_seed_gives_the_same_split_and_a_different_seed_does_not():
    """Reproducibility is why the seed exists; stratification must not collapse it to one
    answer, or `--seed` becomes a flag that does nothing and a rerun cannot be varied."""
    rows = _corpus(SKEWED)
    a = make_splits.stratified_holdout(rows, 40, seed=7)
    assert a == make_splits.stratified_holdout(rows, 40, seed=7)
    assert a != make_splits.stratified_holdout(rows, 40, seed=8)


def test_the_holdout_is_the_requested_size_with_no_duplicates():
    rows = _corpus(SKEWED)
    for n_holdout in (1, 5, 40, 100):
        holdout = make_splits.stratified_holdout(rows, n_holdout, seed=3)
        assert len(holdout) == n_holdout
        assert holdout <= {r["task_id"] for r in rows}


def test_asking_for_more_strata_than_tasks_is_refused_not_silently_shrunk():
    """A holdout quietly smaller than requested is the failure this module already guards
    at the corpus-size level; the stratifier must not reintroduce it one layer down."""
    rows = _corpus([100, 200, 300])
    with pytest.raises(SystemExit) as e:
        make_splits.stratified_holdout(rows, 10, seed=1)
    assert "stratum" in str(e.value)


def test_every_variant_of_a_task_shares_the_length_the_stratifier_reads():
    """The stratifier keeps ONE length per task_id, taking whichever row it meets first.
    That is only sound because a task's variants share a prompt: the augmentation permutes
    colours and applies a geometry, and neither changes the cell count. Measured on the
    real corpus, all 25 variants of a task tokenize its prompt to the identical length
    (the row totals differ only by the wrapper code the variant carries). If that ever
    stops being true, this reads the wrong length for the whole stratum.
    """
    rows = _corpus([500, 900], variants=4)
    per_task = {}
    for r in rows:
        per_task.setdefault(r["task_id"], set()).add(len(r["prompt"]))
    assert all(len(v) == 1 for v in per_task.values())


def test_the_selection_is_not_just_the_shortest_or_the_longest_tasks():
    """A stratifier that took `ordered[:n]` would pass a determinism test and every
    size check above while being maximally biased."""
    rows = _corpus(SKEWED)
    by_task = {}
    for r in rows:
        by_task.setdefault(r["task_id"], len(r["prompt"]))
    holdout = make_splits.stratified_holdout(rows, 40, seed=5)
    ordered = sorted(by_task, key=lambda t: (by_task[t], t))
    assert holdout != set(ordered[:40])
    assert holdout != set(ordered[-40:])
    # and it must span the range, not cluster: the picks straddle the corpus median
    picked = [by_task[t] for t in holdout]
    med = statistics.median(list(by_task.values()))
    assert min(picked) < med < max(picked)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
