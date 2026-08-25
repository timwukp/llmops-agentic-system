#!/usr/bin/env python3
"""Bridge the ARC-AGI-2 public evaluation set into the `val_raw.jsonl` schema.

Why this file has to exist. The plan's MAIN metric is "same base model vs the
fine-tuned one on the 120 public evaluation tasks, pass@2". Nothing in this repo
could read those tasks: `generate_student.py --val` consumes the v2 val split,
whose rows are augmented variants of *training* tasks, and a grep for
`evaluation_challenges` / `eval120` across the tree returns nothing. The metric
had no input file. This produces it, at $0, on CPU.

WHAT IS AND IS NOT IN A PROMPT. Measured, not assumed: the distillation corpus
prompt contains the header, then the training pairs, and NOTHING else -- no test
input, no trailing question. That is the program-synthesis contract. The model
writes `transform(grid)` from the demonstrations alone; the test input is applied
to the emitted program afterwards, by the scorer, and the model never sees it.
So an eval row is buildable from `train` alone and the ARC test pairs go straight
into `heldout_pairs`, which is exactly where `eval_student.py` scores them.

THE TWO TEMPLATES. Reconstructing all 849 corpus prompts from the raw ARC JSON
gives 849/849 byte-identical only with TWO different templates, in these
proportions (they match `gate_report.json`'s `rounds_known 677 / superseded 172`
exactly -- the corpus is two distillation rounds stapled together):

    A   677 source tasks   16,075 / 20,200 training rows   79.6%
    B   172 source tasks    4,125 / 20,200 training rows   20.4%

They differ in three places, not one:

    | | A | B |
    |---|---|---|
    | verb   | "implements the transformation." | "converts input grids to output grids." |
    | types  | absent | "Grid = list of lists of int (0-9). Return a list of lists of int." |
    | format | ABSENT | "Output ONLY the function. No explanation. No comments. ..." |

The third row is the one that matters at inference. Template A -- the 79.6%
majority, and therefore the one the student saw most -- never asks for code-only
output. Prompting a *thinking* model with A licenses prose, and since
`enable_thinking` is a no-op in Qwen3-4B-Thinking-2507's chat template there is
no way to suppress the reasoning block either. B is the template that constrains
the format, and it is the minority.

Hence `--template`, defaulting to `a`, and hence the template id is written into
every row and into the report. Base and fine-tuned MUST be generated with the
same value or the comparison is between two experiments. Nothing here picks the
default on the grounds that it is better; it is picked because it is the majority
of the training distribution, which is the only defensible prior when the point
of the run is to measure what training changed.

Note what this file does NOT claim: it does not claim A is the right template.
`--template b` exists so that question can be answered by measurement later, for
the price of one more generation pass, on the same 120 tasks.

NO `code` FIELD. The evaluation set has no reference solver, and inventing one
would be worse than omitting it: `score_generations` scores the *student's*
program, and the presence of a `code` key is what tells a reader a row came from
distillation. Absent means absent.

CONTAMINATION. These 120 tasks are published on GitHub and shipped in the Kaggle
bundle, so any absolute score here is an UPPER bound. What survives contamination
is the comparison: base and fine-tuned are contaminated identically, so the lift
between them is still a measurement. The report says so in a field, not a
comment, because a number that travels without its caveat loses it.

Usage:
    python3 build_eval120_val.py \
        --challenges arc-agi_evaluation_challenges.json \
        --solutions  arc-agi_evaluation_solutions.json \
        --out        eval120_val_raw.jsonl \
        [--template a|b] [--report report.json] [--corpus <triplets.jsonl>]

`--corpus` turns on the byte-exactness self-check against the real distillation
corpus. It is optional only because the corpus is not on the training instance;
when the file is reachable it should always be passed, and `--require-corpus`
makes a missing one an error instead of a skip.

Exit codes: 0 = rows written. 1 = the renderer self-test failed, zero tasks were
read, a task had no solution, or zero rows were written. A build that emits an
empty file must not exit 0 -- that is how an empty metric gets read as a zero
score instead of as a missing measurement.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

# Named the same way augment.py names V2_ARC_TRAINING_DIR, and deliberately with no
# path constant beside it: augment.py can fall back to /tmp/arc/data/training because
# a missing ARC dir there only costs it the pair metadata it can re-parse out of the
# prompt, whereas here the files ARE the corpus and a wrong one is undetectable.
ARC_EVAL_DIR_ENV = "V2_ARC_EVAL_DIR"

# ── the two templates, verbatim ───────────────────────────────────────────────────
# Byte-exact. Verified by reconstructing all 849 corpus prompts: 849/849 with
# A on 677 and B on 172. Do not "tidy" the punctuation or the newlines; the iron
# rule this experiment's v1 died of is that inference text must be a byte-exact
# prefix of training text, and every character below is load-bearing.
TEMPLATES = {
    "a": {
        "head": ("Solve this ARC task. Write a Python function `transform(grid)` "
                 "that implements the transformation.\n"),
        "tail": "",
        "share_of_train_rows": 0.7959,
        "source_tasks": 677,
        "asks_for_code_only": False,
    },
    "b": {
        "head": ("Solve this ARC task. Write a Python function `transform(grid)` "
                 "that converts input grids to output grids.\n"
                 "Grid = list of lists of int (0-9). Return a list of lists of int.\n"),
        "tail": ("\nOutput ONLY the function. No explanation. No comments. "
                 "Code must be minimal and correct."),
        "share_of_train_rows": 0.2041,
        "source_tasks": 172,
        "asks_for_code_only": True,
    },
}


def render_grid_block(grid: list[list[int]]) -> str:
    """`(HxW):\\n` then space-joined rows then a trailing newline.

    Deliberately a separate function from `augment.py:render_grid_block` even
    though the output is identical, and deliberately not imported from it: that
    one is reached only through `rerender_prompt`, which *rewrites* an existing
    prompt by substituting grid blocks and therefore never has to produce the
    scaffolding between blocks. This file has to build a prompt from nothing, so
    it needs the scaffolding too, and the byte-exactness check below is what ties
    the two together -- if either drifts, 849/849 stops holding.
    """
    return (f"({len(grid)}x{len(grid[0])}):\n"
            + "\n".join(" ".join(str(c) for c in row) for row in grid) + "\n")


def render_prompt(train_pairs: list[dict], template: str) -> str:
    """Build the program-synthesis prompt for one task.

    A blank line separates the header from pair 1 and each pair from the next,
    but there is NO blank line after the final pair in template A -- the prompt
    simply ends with the last grid's newline. Template B's tail supplies the
    blank line itself. This asymmetry is not a style choice; it is what the
    corpus contains, and getting it wrong costs a byte-exactness failure that
    only shows up as a mysteriously worse solve rate.
    """
    tpl = TEMPLATES[template]
    out = [tpl["head"], "\n"]
    for i, p in enumerate(train_pairs, 1):
        out.append(f"Training pair {i}:\n")
        out.append("Input " + render_grid_block(p["input"]))
        out.append("Output " + render_grid_block(p["output"]))
        if i < len(train_pairs):
            out.append("\n")
    out.append(tpl["tail"])
    return "".join(out)


def self_test() -> dict:
    """Prove the renderer both fires and can be caught being wrong.

    An accepts-only check on a renderer is vacuous: a function that returned the
    corpus prompt from a lookup table would pass it. So each assertion below is
    paired with a MUTATION -- the same input rendered with one byte changed must
    NOT compare equal. Without the negative half, "849/849 byte-identical" would
    only prove that a comparison ran.
    """
    pairs = [{"input": [[1, 2], [3, 4]], "output": [[4, 3], [2, 1]]},
             {"input": [[0]], "output": [[5]]}]
    a = render_prompt(pairs, "a")
    b = render_prompt(pairs, "b")
    return {
        # the block format itself
        "block_shape_header": render_grid_block([[1, 2, 3]]) == "(1x3):\n1 2 3\n",
        "block_multirow": render_grid_block([[1], [2]]) == "(2x1):\n1\n2\n",
        # template A: header, blank line, pairs, ends on a grid newline
        "a_starts_with_header": a.startswith(TEMPLATES["a"]["head"] + "\n"),
        "a_ends_on_grid": a.endswith("(1x1):\n5\n"),
        "a_has_no_tail_instruction": "Output ONLY" not in a,
        "a_no_test_input_in_prompt": "Test" not in a and "test" not in a,
        "a_pair_count": a.count("Training pair ") == 2,
        "a_blank_line_between_pairs": "\n\nTraining pair 2:\n" in a,
        # template B: extra type line AND the code-only instruction
        "b_has_type_line": "Grid = list of lists of int (0-9)." in b,
        "b_has_tail_instruction": b.endswith(
            "Output ONLY the function. No explanation. No comments. "
            "Code must be minimal and correct."),
        "b_blank_line_before_tail": "\n\nOutput ONLY the function." in b,
        # the two are genuinely different text, not the same string twice
        "templates_differ": a != b,
        "templates_differ_in_three_places": (
            ("implements the transformation." in a)
            and ("converts input grids to output grids." in b)
            and ("Grid = list of lists" not in a)
            and ("Output ONLY" not in a)),
        # ── mutation half: a wrong renderer must be detectable ──
        "detects_missing_blank_line": a != a.replace("\n\nTraining pair 2:", "\nTraining pair 2:"),
        "detects_wrong_separator": (render_grid_block([[1, 2]]) != "(1x2):\n1,2\n"),
        "detects_wrong_dims_order": (render_grid_block([[1, 2, 3]]) != "(3x1):\n1 2 3\n"),
        "detects_swapped_template": render_prompt(pairs, "a") != render_prompt(pairs, "b"),
    }


def corpus_check(corpus_jsonl: str, arc_challenges: str) -> dict:
    """Rebuild every prompt in the real distillation corpus and demand byte equality.

    This is the check that makes the two-template finding a measurement rather
    than an observation. It is also the only thing standing between this file and
    the v1 failure mode: if `render_prompt` drifts one byte from what the student
    was trained on, the fine-tuned model is asked to continue text it has never
    seen, and the resulting low score gets misread as "distillation has no signal".

    Any task rendered by NEITHER template is reported by id, not swallowed -- a
    third template hiding in the corpus is exactly the kind of thing a
    `sum(matches)` headline conceals.
    """
    ch = json.load(open(arc_challenges))
    rows = [json.loads(line) for line in open(corpus_jsonl) if line.strip()]
    if not rows:
        raise SystemExit(f"corpus check read zero rows from {corpus_jsonl}")
    hits, unmatched, absent = Counter(), [], []
    for r in rows:
        tid = r["task_id"]
        if tid not in ch:
            absent.append(tid)
            continue
        for name in TEMPLATES:
            if render_prompt(ch[tid]["train"], name) == r["prompt"]:
                hits[name] += 1
                break
        else:
            unmatched.append(tid)
    return {
        "corpus": os.path.basename(corpus_jsonl),
        "n_rows": len(rows),
        "byte_identical": sum(hits.values()),
        "by_template": dict(hits),
        "unmatched_task_ids": unmatched[:20],
        "n_unmatched": len(unmatched),
        "n_task_ids_absent_from_arc": len(absent),
        # 849/849 is the pass condition. Anything less means a template is missing
        # from TEMPLATES, and inference would then use text no row was trained on.
        "all_reproduced": len(unmatched) == 0 and len(absent) == 0,
    }


def build_rows(challenges: dict, solutions: dict, template: str) -> list[dict]:
    """One row per TASK, carrying every test input in `heldout_pairs`.

    Per task, NOT per test input, and the reason is not style. 40.8% of the 120
    tasks have more than one test input, but all of a task's test inputs share the
    SAME prompt -- the prompt is built from the training pairs alone. Emitting one
    row per test input would therefore ask the GPU to generate from 52 byte-identical
    prompts a second time: 172 generations for 120 distinct questions, ~30% of the
    eval budget spent re-deriving answers already in hand.

    Worse, it would be unscorable. `generate_student.py` writes only `task_id`,
    `variant` and `sample_idx` per generation; `variant` is 0 on every eval row, so
    two rows of the same multi-test task are indistinguishable in
    `generations.jsonl` and the scorer would join them arbitrarily.

    Nothing is lost by grouping, because `score_generations` already records
    `heldout_pairs_passed` / `heldout_pairs_total` alongside `heldout_solved`. That
    gives BOTH readings off one row: `heldout_solved` is the strict score (every
    test input correct, which is what ARC grades), and the passed/total ratio
    summed over tasks is the per-test-input micro-average. Both must be reported --
    they differ by exactly the multi-test population.
    """
    rows = []
    for tid in sorted(challenges):
        task = challenges[tid]
        sol = solutions.get(tid)
        if sol is None:
            raise SystemExit(f"task {tid} has no solution -- refusing to emit a "
                             f"row whose held-out answer is unknown")
        tests = task["test"]
        if len(sol) != len(tests):
            raise SystemExit(f"task {tid}: {len(tests)} test inputs but "
                             f"{len(sol)} solutions")
        rows.append({
            "task_id": tid,
            # `variant` is 0 for every row: these are the real ARC tasks with no
            # geometric/colour augmentation applied. No row in the v2 val split has
            # variant 0 -- every one there is augmented -- so 0 marks an eval row
            # unambiguously.
            "variant": 0,
            "n_test_inputs": len(tests),
            "prompt": render_prompt(task["train"], template),
            "n_train_pairs": len(task["train"]),
            "heldout_pairs": [{"input": t["input"], "output": expected}
                              for t, expected in zip(tests, sol)],
            # No `verified` / `heldout_ok` / `repair_rounds`: those describe a
            # distilled solver's provenance and there is no solver here. A
            # `verified: true` on an eval row would be a lie with a schema.
            "split": "arc2_eval120",
            "template": template,
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arc-dir", default=None,
                    help=f"directory holding arc-agi_evaluation_{{challenges,"
                         f"solutions}}.json; defaults to ${ARC_EVAL_DIR_ENV}. There is "
                         f"deliberately no path default: this is the HELD-OUT set, and "
                         f"a default that happens to resolve on one machine would "
                         f"silently build the main metric's corpus from whatever ARC "
                         f"file that machine had")
    ap.add_argument("--challenges", default=None)
    ap.add_argument("--solutions", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--template", choices=sorted(TEMPLATES), default="a",
                    help="a = the 79.6%% majority of training rows (default); "
                         "b = the 20.4%% minority, which is the one that asks for "
                         "code-only output. Base and fine-tuned must use the same.")
    ap.add_argument("--report", default=None)
    ap.add_argument("--corpus", default=None,
                    help="distillation triplets jsonl; enables the byte-exactness "
                         "check against real training prompts")
    ap.add_argument("--train-challenges", default=None,
                    help="the ARC file the --corpus task ids live in; defaults to "
                         "arc-agi_training_challenges.json under --arc-dir")
    ap.add_argument("--require-corpus", action="store_true",
                    help="fail instead of skipping when --corpus is absent")
    args = ap.parse_args()

    # Resolved after parsing so --challenges/--solutions can each override the
    # directory individually, and so the error names the flag AND the variable.
    arc_dir = args.arc_dir or os.environ.get(ARC_EVAL_DIR_ENV)
    need_dir = [n for n, v in (("--challenges", args.challenges),
                               ("--solutions", args.solutions)) if not v]
    if need_dir and not arc_dir:
        print(f"no ARC evaluation directory: pass --arc-dir, set "
              f"${ARC_EVAL_DIR_ENV}, or give {' and '.join(need_dir)} explicitly",
              file=sys.stderr)
        return 2
    if not args.challenges:
        args.challenges = os.path.join(arc_dir, "arc-agi_evaluation_challenges.json")
    if not args.solutions:
        args.solutions = os.path.join(arc_dir, "arc-agi_evaluation_solutions.json")
    if args.corpus and not args.train_challenges:
        if not arc_dir:
            print(f"--corpus needs --train-challenges or an ARC directory "
                  f"(--arc-dir / ${ARC_EVAL_DIR_ENV})", file=sys.stderr)
            return 2
        args.train_challenges = os.path.join(arc_dir,
                                             "arc-agi_training_challenges.json")

    st = self_test()
    if not all(st.values()):
        failed = [k for k, v in st.items() if not v]
        print(f"renderer self-test FAILED: {failed}", file=sys.stderr)
        return 1
    print(f"renderer self-test: {len(st)}/{len(st)} pass")

    check = None
    if args.corpus:
        check = corpus_check(args.corpus, args.train_challenges)
        print(f"corpus byte-exactness: {check['byte_identical']}/{check['n_rows']} "
              f"{check['by_template']}")
        if not check["all_reproduced"]:
            print(f"corpus check FAILED: {check['n_unmatched']} unmatched, "
                  f"{check['n_task_ids_absent_from_arc']} absent -- a template is "
                  f"missing from TEMPLATES", file=sys.stderr)
            return 1
    elif args.require_corpus:
        print("--require-corpus given but --corpus was not", file=sys.stderr)
        return 1
    else:
        print("corpus byte-exactness: SKIPPED (no --corpus) -- prompts are "
              "UNVERIFIED against training text")

    challenges = json.load(open(args.challenges))
    solutions = json.load(open(args.solutions))
    if not challenges:
        print(f"{args.challenges} loaded zero tasks", file=sys.stderr)
        return 1

    rows = build_rows(challenges, solutions, args.template)
    if not rows:
        print("zero rows built", file=sys.stderr)
        return 1

    with open(args.out, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    multi = sum(1 for r in rows if r["n_test_inputs"] > 1)
    n_inputs = sum(r["n_test_inputs"] for r in rows)
    lens = sorted(len(r["prompt"]) for r in rows)
    if len(rows) != len(challenges):
        print(f"one row per task expected, got {len(rows)} for "
              f"{len(challenges)} tasks", file=sys.stderr)
        return 1
    report = {
        "out": args.out,
        "n_tasks": len(challenges),
        "n_rows": len(rows),
        "multi_test_tasks": multi,
        "n_test_inputs_total": n_inputs,
        # The two denominators, side by side, because the strict and micro-average
        # scores are computed over different ones and quoting either alone invites
        # the reader to assume the other.
        "denominators": {"strict_score": len(rows),
                         "per_test_input_micro_average": n_inputs},
        "generations_saved_vs_per_input_rows": n_inputs - len(rows),
        "template": args.template,
        "template_share_of_train_rows": TEMPLATES[args.template]["share_of_train_rows"],
        "template_asks_for_code_only": TEMPLATES[args.template]["asks_for_code_only"],
        "prompt_chars": {"min": lens[0], "median": lens[len(lens) // 2],
                         "p90": lens[int(.9 * len(lens))], "max": lens[-1]},
        "self_test": st,
        "corpus_check": check,
        # Stated as data, not prose, so it cannot be separated from the numbers.
        "contamination_caveat": (
            "The 120 public evaluation tasks are published on GitHub and shipped "
            "in the Kaggle bundle. Absolute solve rates from this file are UPPER "
            "bounds. Base-vs-fine-tuned lift remains valid: both sides are "
            "contaminated identically."),
        "scoring_note": (
            "One row per task, all test inputs in heldout_pairs. eval_student.py "
            "score_generations gives BOTH readings from this: heldout_solved is the "
            "strict score (every test input correct -- what ARC grades), and "
            "sum(heldout_pairs_passed)/sum(heldout_pairs_total) is the "
            "per-test-input micro-average. Report both; they diverge exactly on the "
            "multi-test population."),
    }
    if args.report:
        json.dump(report, open(args.report, "w"), indent=2)
    print(json.dumps({k: v for k, v in report.items()
                      if k not in ("self_test", "corpus_check")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
