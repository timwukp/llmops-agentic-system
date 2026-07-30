"""Score a student model's generated ARC `transform` code by EXECUTING it.

The eval gate for this experiment is verifiable, not judged: a generation counts
as solved only when its code reproduces every training pair in the prompt, cell for
cell, in the sandbox. No LLM judge, no ROUGE, no partial credit for looking right.

Two stages, deliberately separated so scoring needs no GPU:

  generate (GPU, SageMaker)   prompt -> raw model text          -> generations.jsonl
  score    (CPU, anywhere)    generations.jsonl + val_raw.jsonl -> eval_report.json

Scoring reconstructs the ground-truth pairs from the prompt text itself, so a
generations file only has to carry `task_id`, `variant`, and `generation`.

Usage:
    python eval_student.py score --generations gen.jsonl --val out/val_raw.jsonl \
        --out eval_report.json [--teacher-report teacher.json] [--gate-ratio 0.80]

    # oracle check: feed the verified ground-truth code back in; must score 1.000
    python eval_student.py self-test --val out/val_raw.jsonl [--sample 60]

Exits non-zero when a teacher report is supplied and the student misses the gate,
or when the self-test scores anything below a perfect 1.0.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_sandbox import verify_code  # noqa: E402

GRID_HEADER = re.compile(r"^(Input|Output) \((\d+)x(\d+)\):$")
FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)(?:```|\Z)", re.DOTALL)


def parse_pairs(prompt: str) -> list[dict]:
    """Recover [{'input': grid, 'output': grid}] from a rendered ARC prompt.

    Grids are space-separated digit rows under an `Input (HxW):` / `Output (HxW):`
    header. The declared HxW is checked against the rows actually parsed — a
    mismatch means the prompt is malformed and scoring it would be meaningless.
    """
    pairs, current, key = [], {}, None
    expect = None

    for line in prompt.split("\n"):
        header = GRID_HEADER.match(line)
        if header:
            if key and expect:          # a header also closes the preceding grid
                _check_shape(current[key], expect, key)
            key = header.group(1).lower()
            current[key] = []
            expect = (int(header.group(2)), int(header.group(3)))
            continue
        if key and re.fullmatch(r"\d+(?: \d+)*", line):
            current[key].append([int(c) for c in line.split(" ")])
            continue
        # Any other non-empty line ends the current grid.
        if line.strip():
            if key and expect:
                _check_shape(current[key], expect, key)
            key, expect = None, None
        if key is None and {"input", "output"} <= current.keys():
            pairs.append(current)
            current = {}

    if key and expect:
        _check_shape(current[key], expect, key)
    if {"input", "output"} <= current.keys():
        pairs.append(current)
    return pairs


def _check_shape(grid, expect, key):
    h, w = expect
    if len(grid) != h or any(len(r) != w for r in grid):
        raise ValueError(f"{key} grid declares {h}x{w} but parsed "
                         f"{len(grid)}x{len(grid[0]) if grid else 0}")


def extract_code(text: str) -> str | None:
    """Pull the Python source out of a raw generation.

    Handles a fenced block, an unfenced block starting at a `def`/`import` line,
    and a `<think>` prefix. Returns None when there is no `transform` definition
    to run — that counts as a format failure, never as a wrong answer.
    """
    if not text:
        return None
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)

    fenced = FENCE.search(text)
    candidate = fenced.group(1) if fenced else _from_first_code_line(text)
    if not candidate or not re.search(r"^\s*def transform\s*\(", candidate, re.M):
        return None
    return candidate


def _from_first_code_line(text: str) -> str | None:
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if re.match(r"^(def |import |from |@)", line):
            return "\n".join(lines[i:])
    return None


def score_generations(generations: list[dict], val_rows: list[dict],
                      timeout_sec: int = 5) -> dict:
    """Execute every generation against its task's pairs; exact match only."""
    by_key = {(r["task_id"], r.get("variant", "")): r for r in val_rows}
    results, n_solved, n_parsed, n_truncated = [], 0, 0, 0

    for gen in generations:
        key = (gen["task_id"], gen.get("variant", ""))
        row = by_key.get(key)
        if row is None:
            results.append({**key_fields(key), "status": "no_matching_val_row"})
            continue

        code = extract_code(gen.get("generation", ""))
        if code is None:
            # A generation that ran out of tokens mid-answer is a budget artefact,
            # not evidence the model cannot write code. Keep the two apart.
            n_truncated += bool(gen.get("truncated"))
            results.append({**key_fields(key), "status": "no_transform_emitted",
                            "solved": False,
                            "truncated": bool(gen.get("truncated"))})
            continue
        n_parsed += 1

        try:
            pairs = parse_pairs(row["prompt"])
        except ValueError as exc:  # malformed prompt — surface, don't score
            results.append({**key_fields(key), "status": f"bad_prompt: {exc}"})
            continue

        verdict = verify_code(code, pairs, timeout_sec=timeout_sec)
        solved = bool(verdict["all_pass"])
        n_solved += solved
        results.append({**key_fields(key),
                        "status": "solved" if solved else "failed_verification",
                        "solved": solved,
                        "pairs_passed": verdict["n_pass"],
                        "pairs_total": verdict["n_pairs"],
                        "fail_reason": verdict["fail_reason"]})

    scored = [r for r in results if "solved" in r]
    n = len(scored)
    report = {
        "n_generations": len(generations),
        "n_scored": n,
        "n_format_valid": n_parsed,
        "n_solved": n_solved,
        "n_truncated_format_failures": n_truncated,
        "solve_rate": n_solved / n if n else 0.0,
        "format_valid_rate": n_parsed / n if n else 0.0,
        "results": results,
    }
    if n_truncated:
        report["format_caveat"] = (
            f"{n_truncated} of the {n - n_parsed} format failures ran out of "
            f"generation tokens mid-answer, so format_valid_rate "
            f"({report['format_valid_rate']:.3f}) partly measures the token budget, "
            f"not the model; raise --max-new-tokens before reading it as ability")
    return report


def key_fields(key):
    return {"task_id": key[0], "variant": key[1]}


def apply_gate(student: dict, teacher: dict | None, ratio: float) -> dict:
    """Gate the student against a teacher baseline measured on the same rows.

    A caution about what the baseline can mean on THIS val set: every row exists
    only because a teacher solution was found and sandbox-verified for its source
    task, so a teacher re-measured here scores ~1.0 by construction and the
    "relative" gate silently degenerates into an absolute `ratio` bar. That is a
    selection effect in the data, not a property of the teacher.

    So a baseline is only informative when it is measured on rows chosen
    independently of whether a solution was found — e.g. the untouched ARC-AGI-2
    evaluation split. When the baseline is >= `degenerate_above`, the report says
    so in `baseline_caveat` instead of letting a 0.80x label imply a comparison
    it isn't making.
    """
    degenerate_above = 0.99
    gate = {"gate_ratio": ratio, "student_solve_rate": student["solve_rate"]}
    if not teacher:
        gate.update(status="NO_TEACHER_BASELINE", passed=None)
        return gate

    t = teacher["solve_rate"]
    threshold = ratio * t
    gate.update(teacher_solve_rate=t, threshold=threshold,
                passed=student["solve_rate"] >= threshold)
    gate["status"] = "PASSED" if gate["passed"] else "FAILED"

    if t >= degenerate_above:
        gate["baseline_caveat"] = (
            f"teacher solve rate is {t:.3f}; on a val set built from verified "
            f"solutions this is expected by construction, so the gate is really an "
            f"absolute {threshold:.1%} bar, not a {ratio:.0%} comparison")
    elif t == 0.0:
        gate["baseline_caveat"] = (
            "teacher solve rate is 0.000, so any student passes arithmetically; "
            "the gate carries no quality signal")
    return gate


def compute_lift(student: dict, base: dict | None) -> dict | None:
    """Compare the fine-tuned student against the SAME model before fine-tuning.

    This is the measurement that actually answers "did the distillation do
    anything", and unlike the teacher baseline it cannot be inflated by how the
    val set was selected: the selection effect that hands the teacher ~1.0 applies
    identically to both sides here, so it cancels.

    Both rates come from the same prompts, the same greedy decoding, and the same
    executable scoring — the only difference is the weights.
    """
    if not base:
        return None
    s, b = student["solve_rate"], base["solve_rate"]
    lift = {
        "base_solve_rate": b,
        "student_solve_rate": s,
        "absolute_gain": s - b,
        "relative_gain": (s - b) / b if b else None,
        "base_format_valid_rate": base.get("format_valid_rate"),
        "student_format_valid_rate": student.get("format_valid_rate"),
    }
    if s > b:
        lift["verdict"] = "fine-tuning improved executable correctness"
    elif s == b:
        lift["verdict"] = "no change in executable correctness"
    else:
        lift["verdict"] = "REGRESSION — fine-tuning made executable correctness worse"

    # For a 1.7B model on ARC-AGI-2, both rates can legitimately be 0. Say so
    # rather than reporting a 0.0 gain as though it settled the question.
    if s == 0 and b == 0:
        fs = student.get("format_valid_rate") or 0
        fb = base.get("format_valid_rate") or 0
        lift["verdict"] = ("both solve rates are 0.000, so solve rate cannot "
                           "distinguish them; compare format_valid_rate instead "
                           f"({fb:.3f} -> {fs:.3f})")
    return lift


def read_jsonl(path: str) -> list[dict]:
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def self_test(val_rows: list[dict], sample: int, timeout_sec: int) -> int:
    """Score the verified ground-truth code as if the model had emitted it.

    Anything below 1.000 means the scorer is rejecting known-correct solutions, so
    every student number it produces would understate the model. Run this before
    trusting any eval report.
    """
    import random
    random.seed(20260731)
    rows = random.sample(val_rows, min(sample, len(val_rows)))
    gens = [{"task_id": r["task_id"], "variant": r["variant"],
             "generation": f"Here is the solution:\n```python\n{r['code']}\n```"}
            for r in rows]
    rep = score_generations(gens, val_rows, timeout_sec)

    print(f"oracle: solved {rep['n_solved']}/{rep['n_scored']} "
          f"(solve_rate {rep['solve_rate']:.3f}, format {rep['format_valid_rate']:.3f})")
    for bad in (r for r in rep["results"] if not r.get("solved")):
        print(f"  REJECTED KNOWN-GOOD {bad['task_id']} {bad['status']}: "
              f"{str(bad.get('fail_reason'))[:160]}")
    ok = rep["solve_rate"] == 1.0 and rep["format_valid_rate"] == 1.0
    print("PASS — scorer credits every verified solution" if ok
          else "FAIL — the scorer rejects known-correct code; fix it before evaluating")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["score", "self-test"])
    ap.add_argument("--generations", help="required for `score`")
    ap.add_argument("--val", required=True, help="val_raw.jsonl (prompts + task ids)")
    ap.add_argument("--out", help="required for `score`")
    ap.add_argument("--teacher-report", default=None,
                    help="a prior eval_report.json to gate against")
    ap.add_argument("--base-report", default=None,
                    help="eval_report.json for the SAME model before fine-tuning; "
                         "this is the comparison that measures distillation lift")
    ap.add_argument("--gate-ratio", type=float, default=0.80)
    ap.add_argument("--timeout-sec", type=int, default=5)
    ap.add_argument("--sample", type=int, default=60, help="self-test sample size")
    args = ap.parse_args()

    val_rows = read_jsonl(args.val)
    if args.command == "self-test":
        return self_test(val_rows, args.sample, args.timeout_sec)

    if not args.generations or not args.out:
        ap.error("`score` requires --generations and --out")

    report = score_generations(read_jsonl(args.generations), val_rows, args.timeout_sec)
    teacher = json.loads(Path(args.teacher_report).read_text()) if args.teacher_report else None
    base = json.loads(Path(args.base_report).read_text()) if args.base_report else None
    report["gate"] = apply_gate(report, teacher, args.gate_ratio)
    report["lift"] = compute_lift(report, base)

    Path(args.out).write_text(json.dumps(report, indent=2))

    print(f"scored {report['n_scored']}/{report['n_generations']} generations")
    print(f"  format-valid : {report['n_format_valid']} ({report['format_valid_rate']:.1%})")
    print(f"  solved       : {report['n_solved']} ({report['solve_rate']:.1%})")
    if report.get("format_caveat"):
        print(f"  TRUNCATED    : {report['format_caveat']}")
    print(f"  gate         : {report['gate']['status']}")
    if report["gate"].get("baseline_caveat"):
        print(f"  CAVEAT       : {report['gate']['baseline_caveat']}")
    if report["lift"]:
        print(f"  vs base      : {report['lift']['base_solve_rate']:.1%} -> "
              f"{report['lift']['student_solve_rate']:.1%} "
              f"({report['lift']['absolute_gain']:+.1%})")
        print(f"  lift verdict : {report['lift']['verdict']}")
    else:
        print("  vs base      : not measured (pass --base-report for distillation lift)")
    return 0 if report["gate"].get("passed") is not False else 1


if __name__ == "__main__":
    sys.exit(main())
