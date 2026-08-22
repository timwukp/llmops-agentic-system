"""Attach held-out pairs to a triplets corpus and gate every solver on them.

`verified: true` on a distilled solver means "reproduces the training pairs that
were in the prompt". That is a tautology, not a measurement: the generator saw
those pairs. A solver can reproduce all of them and still encode the wrong rule.

Measured on the real ARC training corpus (n=742 train-verified solvers, from the
prototype run's `distill_results.json`), split by how the solver was obtained:

    clean single sample        439/463 = 94.8%  also correct on the unseen test
    repaired after feedback    238/279 = 85.3%  (monotone in repair rounds:
                                                1 -> 94.8%, 2 -> 90.2%,
                                                3 -> 78.7%, 7 -> 66.7%)
    overall                    677/742 = 91.2%

So roughly 1 in 11 shown-pair-verified solvers is a wrong program, and the rate
roughly triples for solvers patched after being told which pair mismatched --
handed "pair 1: output mismatch", a model has a cheaper move than finding the
rule. Those solvers then reach augment.py, whose wrapper algebra preserves the
base program's semantics exactly and therefore replicates a wrong rule across
all 25 variants with a 0.0% rejection rate.

This script closes that door before the money is spent on training. It reads a
`(task_id, prompt, code)` corpus, attaches each task's ARC test pairs -- which
the solver's author never saw -- as `heldout_pairs`, executes the solver against
them in the sandbox, and emits only the rows that pass. Provenance (`repair_rounds`)
travels with each row as a TAG, not a filter: once the gate is applied both
populations are held-out-correct by definition, and excluding the repaired ones
would throw away the hardest tasks for no measured gain.

Usage:
    python3 build_heldout_source.py \
        --source  <triplets.jsonl> \
        --challenges arc-agi_training_challenges.json \
        --solutions  arc-agi_training_solutions.json \
        --out     <gated_source.jsonl> \
        [--provenance distill_results.json] [--report report.json] [--workers K]

Exit codes: 0 = rows survived; 1 = the sandbox self-check failed, no source rows
were read, no task ids matched the ARC files, or nothing survived the gate. A
build that produces an empty corpus must not look like a successful one.
"""
from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_sandbox import verify_code  # noqa: E402

TIMEOUT_SEC = 5
Z_95 = 1.96


def wilson(k: int, n: int, z: float = Z_95) -> tuple[float, float] | None:
    """Wilson score interval for k/n, unrounded.

    A third copy of the formula that `deploy/console/lambda_function.py` and
    `tools/probe_protocol_reliability.py` already carry, for the reason stated
    there: those are separately deployed bundles with no shared module to import
    from, and pipeline/v2 is uploaded to SageMaker as its own sourcedir. The
    precedent's condition applies -- `test_heldout_gate.py` compares this
    implementation against the tool's over a table of inputs to 1e-12 and turns
    red the day they disagree.

    Returned unrounded so that comparison can actually be made at 1e-12; the
    rounding a report wants happens at the report, where `wilson95` does it. A
    function that rounds internally can only ever be checked to its own
    precision, which would make the promise above unkeepable.
    """
    if n <= 0:
        return None
    p = k / n
    d = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(max(p * (1 - p), 0.0) / n + z * z / (4 * n * n)) / d
    return centre - half, centre + half


def wilson95(k: int, n: int) -> list[float] | None:
    """Report form: 4 decimal places, JSON-serialisable."""
    iv = wilson(k, n)
    return None if iv is None else [round(iv[0], 4), round(iv[1], 4)]


# --------------------------------------------------------------------------
# The gate has to be able to say no
# --------------------------------------------------------------------------

SELF_CHECK_PAIRS = [{"input": [[1, 2]], "output": [[2, 4]]},
                    {"input": [[3]], "output": [[6]]}]
SELF_CHECK_GOOD = "def transform(grid):\n    return [[c * 2 for c in row] for row in grid]\n"
# One character different: * -> +. Still runs, still returns a grid of the right
# shape, and is wrong on both pairs.
SELF_CHECK_CORRUPT = "def transform(grid):\n    return [[c + 2 for c in row] for row in grid]\n"
SELF_CHECK_HANG = "def transform(grid):\n    while True:\n        pass\n"


def gate_self_check() -> dict:
    """Prove the verifier accepts a correct solver and rejects two wrong ones.

    Without this, a verifier broken in the permissive direction (or a sandbox
    whose SIGALRM never fires) reports every solver held-out-correct and the gate
    becomes an expensive no-op that looks like good news. Three controls, run
    before a single source row is touched: accept the right answer, reject a
    one-character corruption of it, reject an infinite loop.
    """
    checks = {
        "accepts_correct": verify_code(SELF_CHECK_GOOD, SELF_CHECK_PAIRS,
                                      TIMEOUT_SEC)["all_pass"] is True,
        "rejects_corruption": verify_code(SELF_CHECK_CORRUPT, SELF_CHECK_PAIRS,
                                          TIMEOUT_SEC)["all_pass"] is False,
        "rejects_infinite_loop": verify_code(SELF_CHECK_HANG, SELF_CHECK_PAIRS,
                                             TIMEOUT_SEC)["all_pass"] is False,
    }
    return checks


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

def load_arc_heldout(challenges_path: str, solutions_path: str) -> dict:
    """task_id -> [{'input','output'}] for the ARC *test* pairs.

    The challenges file carries the test inputs and the solutions file the
    matching outputs; a task can have more than one test pair (69 of the 1,000
    training tasks do), and all of them are used. Gating on one and ignoring the
    rest would let a solver that handles only the first input through.
    """
    with open(challenges_path) as fh:
        challenges = json.load(fh)
    with open(solutions_path) as fh:
        solutions = json.load(fh)

    heldout = {}
    for task_id, task in challenges.items():
        outs = solutions.get(task_id)
        if outs is None:
            continue
        tests = task.get("test", [])
        if len(tests) != len(outs):
            raise SystemExit(
                f"{task_id}: {len(tests)} test inputs but {len(outs)} solutions. "
                f"The two files disagree; pairing them by index would silently "
                f"verify against the wrong grid.")
        heldout[task_id] = [{"input": t["input"], "output": o}
                            for t, o in zip(tests, outs)]
    return heldout


def load_provenance(path: str | None) -> dict:
    """task_id -> (repair rounds, the code those rounds produced).

    The code is carried because `task_id` is NOT a key for this join. A distillation
    run records one entry per task, but a corpus is assembled over several runs, and a
    later pass may REPLACE a task's solver wholesale -- keeping the task_id and
    invalidating the round count. Measured on
    `training_pairs_perfect_849.jsonl` against `distill_output/distill_results.json`:
    only 676 of 848 rows carry the code their provenance entry describes, and among
    the entries recording `rounds_used: 10` (the loop cap) just 6 of 154 do. Tagging
    those 148 rows `10` would attribute a superseded solver's effort to the solver
    actually in the corpus -- and it showed up as `10 -> 154/154 = 100%`, sitting in
    the report as evidence AGAINST the dose-response the same file documents.
    """
    if not path:
        return {}
    with open(path) as fh:
        data = json.load(fh)
    rows = data["results"] if isinstance(data, dict) and "results" in data else data
    items = rows.values() if isinstance(rows, dict) else rows
    return {r["task_id"]: (r.get("rounds_used"), r.get("code"))
            for r in items if r.get("rounds_used") is not None}


#: What a row's `repair_rounds` says when the join cannot be trusted. Kept distinct from
#: `"unknown"` because they call for different work: `unknown` means nobody recorded the
#: effort, `superseded` means someone did and it was spent on a DIFFERENT program.
SUPERSEDED = "superseded"
UNKNOWN = "unknown"


def repair_rounds_for(row: dict, provenance: dict):
    """The round count, only when the entry describes the code in THIS row."""
    entry = provenance.get(row["task_id"])
    if entry is None:
        return UNKNOWN
    rounds, code = entry
    if code is None or code != row.get("code"):
        return SUPERSEDED
    return rounds


# --------------------------------------------------------------------------
# Per-row gate
# --------------------------------------------------------------------------

def check_row(args: tuple[dict, list[dict]]) -> dict:
    row, heldout = args
    verdict = verify_code(row["code"], heldout, TIMEOUT_SEC)
    return {"task_id": row["task_id"], "ok": bool(verdict["all_pass"]),
            "n_pass": verdict["n_pass"], "n_pairs": verdict["n_pairs"],
            "fail_reason": verdict["fail_reason"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True,
                    help="triplets jsonl with task_id / prompt / code")
    ap.add_argument("--challenges", required=True,
                    help="ARC challenges json (test INPUTS)")
    ap.add_argument("--solutions", required=True,
                    help="ARC solutions json (test OUTPUTS -- the held-out labels)")
    ap.add_argument("--out", required=True, help="gated source jsonl to write")
    ap.add_argument("--rejects-out", default=None,
                    help="where to write the rows that failed the gate, with "
                         "reasons (default: <out>.rejected.jsonl). They are "
                         "evidence about the teacher, not garbage")
    ap.add_argument("--provenance", default=None,
                    help="distill_results.json, to tag rows with repair_rounds")
    ap.add_argument("--report", default=None,
                    help="gate report json (default: <out>.report.json)")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    args = ap.parse_args()

    checks = gate_self_check()
    print("gate self-check: " + ", ".join(f"{k}={v}" for k, v in checks.items()))
    if not all(checks.values()):
        print("FAIL: the verifier cannot be trusted to reject a wrong solver, so "
              "any gate it applies is decoration. Nothing was written.",
              file=sys.stderr)
        return 1

    rows = [json.loads(line) for line in open(args.source) if line.strip()]
    if not rows:
        print(f"FAIL: read 0 rows from {args.source}", file=sys.stderr)
        return 1

    heldout_by_task = load_arc_heldout(args.challenges, args.solutions)
    provenance = load_provenance(args.provenance)

    matched = [(r, heldout_by_task[r["task_id"]]) for r in rows
               if r["task_id"] in heldout_by_task]
    unmatched = [r["task_id"] for r in rows if r["task_id"] not in heldout_by_task]
    print(f"source rows: {len(rows)}, held-out pairs found for {len(matched)}, "
          f"no ARC entry for {len(unmatched)}")
    if not matched:
        print("FAIL: no source task_id appears in the ARC files. The corpus and "
              "the challenge files describe different tasks -- check that the "
              "source is the ARC-derived one and not a synthetic corpus.",
              file=sys.stderr)
        return 1

    t0 = time.time()
    if args.workers > 1:
        ctx = mp.get_context("fork" if sys.platform != "win32" else "spawn")
        with ctx.Pool(args.workers) as pool:
            verdicts = list(pool.imap(check_row, matched, chunksize=4))
    else:
        verdicts = [check_row(m) for m in matched]

    by_task = {v["task_id"]: v for v in verdicts}
    kept, rejected = [], []
    for row, heldout in matched:
        v = by_task[row["task_id"]]
        rounds = repair_rounds_for(row, provenance)
        if v["ok"]:
            kept.append({**row,
                         "heldout_pairs": heldout,
                         "heldout_ok": True,
                         "n_heldout_pairs": len(heldout),
                         "repair_rounds": rounds})
        else:
            rejected.append({"task_id": row["task_id"], "repair_rounds": rounds,
                             "heldout_pairs_passed": v["n_pass"],
                             "heldout_pairs_total": v["n_pairs"],
                             "fail_reason": v["fail_reason"],
                             "code": row["code"]})

    out_path = args.out
    with open(out_path, "w") as fh:
        for r in kept:
            fh.write(json.dumps(r) + "\n")
    rej_path = args.rejects_out or f"{out_path}.rejected.jsonl"
    with open(rej_path, "w") as fh:
        for r in rejected:
            fh.write(json.dumps(r) + "\n")

    # By repair rounds: the dose-response is the whole argument for the gate, so
    # it is measured here on whatever corpus is being built rather than quoted
    # from the run that first found it.
    by_rounds = defaultdict(lambda: [0, 0])
    for row, _ in matched:
        rounds = repair_rounds_for(row, provenance)
        by_rounds[str(rounds)][1] += 1
        by_rounds[str(rounds)][0] += by_task[row["task_id"]]["ok"]
    rounds_table = {k: {"heldout_correct": v[0], "n": v[1],
                        "rate": round(v[0] / v[1], 4) if v[1] else None,
                        "wilson95": wilson95(v[0], v[1])}
                    for k, v in sorted(by_rounds.items())}

    # How much of the corpus the dose-response above actually describes. Reported
    # rather than left implicit: a table whose largest bucket is `superseded` still
    # renders as a table, and the reader has no way to tell from the rates alone.
    tagged = Counter(("dated" if isinstance(repair_rounds_for(row, provenance), int)
                      else repair_rounds_for(row, provenance))
                     for row, _ in matched)
    prov = {"rounds_known": tagged["dated"], SUPERSEDED: tagged[SUPERSEDED],
            UNKNOWN: tagged[UNKNOWN]}
    if tagged[SUPERSEDED]:
        prov["note"] = (
            f"{tagged[SUPERSEDED]} of {len(matched)} rows carry a provenance entry whose "
            f"code is NOT the code in the row: a later pass replaced the solver and kept "
            f"the task_id, so the recorded round count belongs to a program that is no "
            f"longer here. Those rows are tagged '{SUPERSEDED}' rather than given the "
            f"stale number, and the by_repair_rounds rates describe only the "
            f"{tagged['dated']} rows where the join holds.")

    n = len(matched)
    report = {
        "source": args.source,
        "gate_self_check": checks,
        "source_rows": len(rows),
        "no_arc_entry": len(unmatched),
        "no_arc_entry_task_ids": sorted(unmatched)[:50],
        "gated": n,
        "heldout_correct": len(kept),
        "heldout_correct_rate": round(len(kept) / n, 4) if n else None,
        "heldout_correct_wilson95": wilson95(len(kept), n),
        "rejected": len(rejected),
        "provenance": prov,
        "by_repair_rounds": rounds_table,
        "reject_reason_kinds": dict(Counter(
            (r["fail_reason"] or "").split(":")[1].strip() if r["fail_reason"]
            and ":" in r["fail_reason"] else "unknown" for r in rejected)),
        "multi_test_tasks": sum(1 for _, h in matched if len(h) > 1),
        "elapsed_sec": round(time.time() - t0, 1),
        "out": out_path,
        "rejects_out": rej_path,
    }
    rep_path = args.report or f"{out_path}.report.json"
    with open(rep_path, "w") as fh:
        json.dump(report, fh, indent=2)

    print(json.dumps({k: v for k, v in report.items()
                      if k != "no_arc_entry_task_ids"}, indent=2))
    print(f"wrote {out_path} ({len(kept)} rows), {rej_path} ({len(rejected)} rows), "
          f"{rep_path}")

    if not kept:
        print(f"FAIL: 0 of {n} solvers reproduced their held-out pairs. Either the "
              f"corpus is worthless or the pairing is wrong -- an empty corpus "
              f"must not exit 0.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
