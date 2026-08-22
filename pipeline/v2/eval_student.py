"""Score a student model's generated ARC `transform` code by EXECUTING it.

The eval gate for this experiment is verifiable, not judged: a generation counts
as solved only when its code reproduces every training pair in the prompt, cell for
cell, in the sandbox. No LLM judge, no ROUGE, no partial credit for looking right.

Two stages, deliberately separated so scoring needs no GPU:

  generate (GPU, SageMaker)   prompt -> raw model text          -> generations.jsonl
  score    (CPU, anywhere)    generations.jsonl + val_raw.jsonl -> eval_report.json

Scoring reconstructs the ground-truth pairs from the prompt text itself, so a
generations file only has to carry `task_id`, `variant`, and `generation`.

TWO rates, because "solved" has two meanings and only one of them is the question:

  solve_rate          the code reproduces the pairs the PROMPT SHOWED the model.
                      Measures whether the student writes runnable, plausible
                      programs. It cannot measure whether it found the rule --
                      the examples it is checked against are the examples it was
                      given, so a program that overfits them scores as solved.
                      This is the same tautology the teacher gate had, and it sat
                      in this scorer while being used to judge the student.
  heldout_solve_rate  the code is also right on `heldout_pairs` -- pairs carried
                      per row from the source corpus (build_heldout_source.py ->
                      augment.py -> make_splits.py) that no generator ever saw.
                      This is the ARC question.

Their difference is the student's overfit tax, reported as `overfit_gap` over the
one subset where both are defined. When no val row carries held-out pairs the
report says so in `heldout_caveat` rather than letting solve_rate stand unqualified.

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
    # Held-out counters. Kept over their OWN denominator -- rows that actually
    # carry unseen pairs -- and the shown-pair rate is recomputed over that same
    # subset, because a gap between two rates measured on different row sets is
    # not a gap.
    n_heldout_avail = n_heldout_solved = n_shown_solved_in_subset = 0
    n_shown_pass_heldout_fail = 0
    # Emitted a `def transform(` line that does not compile. Reported separately
    # because it is neither "wrote nothing" nor "wrote a wrong program".
    n_unparseable = 0
    # Counted across EVERY status, unlike output truncation. A row whose prompt was
    # cut never saw its whole task, so it can also come back format-valid and fail
    # verification -- that row looks like a wrong program when it is a wrong input.
    n_prompt_cut = n_prompt_cut_solved = 0

    for gen in generations:
        key = (gen["task_id"], gen.get("variant", ""))
        row = by_key.get(key)
        if row is None:
            results.append({**key_fields(key), "status": "no_matching_val_row"})
            continue
        prompt_cut = bool(gen.get("prompt_truncated"))
        n_prompt_cut += prompt_cut

        code = extract_code(gen.get("generation", ""))
        # Code that does not COMPILE is a format failure, not a wrong answer.
        # extract_code only regex-matches a `def transform(` line, so a body cut off
        # mid-expression carries the signature and nothing runnable. Counting it
        # format-valid inflates the two places that number is load-bearing: the
        # verdict compute_lift falls back to when both solve rates are 0 (entirely
        # expected for a 1.7B student here), and the pipeline's `format_validity:
        # 0.95` gate. Measured against the real val set before this check existed:
        # `def transform(grid):\n out = [[c for c in row] for row in gr` scored
        # format_valid 1.000. Compiling here rather than in the sandbox keeps the
        # distinction where it belongs -- a SyntaxError is a property of the text,
        # not of any input pair, and the sandbox reports it once per pair as an
        # execution error indistinguishable from a runtime crash.
        syntax_err = None
        if code is not None:
            try:
                compile(code, "<generation>", "exec")
            except SyntaxError as exc:
                syntax_err = f"{type(exc).__name__}: {exc.msg} (line {exc.lineno})"
        if code is None or syntax_err:
            # A generation that ran out of tokens mid-answer is a budget artefact,
            # not evidence the model cannot write code. Keep the two apart. This is
            # the dominant source of unparseable code: the body stops mid-expression
            # exactly because the budget ran out, so it earns the same caveat.
            n_truncated += bool(gen.get("truncated"))
            n_unparseable += bool(syntax_err)
            results.append({**key_fields(key),
                            "status": "unparseable_code" if syntax_err
                                      else "no_transform_emitted",
                            "solved": False,
                            # Grouped with its siblings under pass@k: a task whose
                            # first attempt emitted nothing and whose second solved
                            # it is a solved task, not half a failure.
                            "sample_idx": gen.get("sample_idx", 0),
                            "truncated": bool(gen.get("truncated")),
                            "prompt_truncated": prompt_cut,
                            "fail_reason": syntax_err})
            continue
        n_parsed += 1

        try:
            pairs = parse_pairs(row["prompt"])
        except ValueError as exc:  # malformed prompt — surface, don't score
            results.append({**key_fields(key), "status": f"bad_prompt: {exc}"})
            continue
        if not pairs:
            # Parsed cleanly but yielded nothing to check against. Excluded from the
            # denominator (no "solved" key) rather than counted as a failure:
            # scoring it either way attributes a data defect to the model. The
            # matching guard in verify_code stops the same case reading as SOLVED.
            results.append({**key_fields(key), "status": "no_pairs_in_prompt"})
            continue

        verdict = verify_code(code, pairs, timeout_sec=timeout_sec)
        solved = bool(verdict["all_pass"])
        n_solved += solved
        n_prompt_cut_solved += (prompt_cut and solved)
        record = {**key_fields(key),
                  "status": "solved" if solved else "failed_verification",
                  "solved": solved,
                  "prompt_truncated": prompt_cut,
                  "pairs_passed": verdict["n_pass"],
                  "pairs_total": verdict["n_pairs"],
                  "fail_reason": verdict["fail_reason"]}

        # The unseen pairs, when the corpus carries them. Run INDEPENDENTLY of the
        # shown-pair verdict rather than only on rows that already passed: the
        # held-out rate is meant to answer "did it get the real answer right", and
        # conditioning it on the shown-pair result would make it a rate over a
        # subset chosen by the very metric it exists to audit.
        heldout = row.get("heldout_pairs") or []
        if heldout:
            hv = verify_code(code, heldout, timeout_sec=timeout_sec)
            h_solved = bool(hv["all_pass"])
            n_heldout_avail += 1
            n_heldout_solved += h_solved
            n_shown_solved_in_subset += solved
            n_shown_pass_heldout_fail += (solved and not h_solved)
            record.update(heldout_solved=h_solved,
                          heldout_pairs_passed=hv["n_pass"],
                          heldout_pairs_total=hv["n_pairs"],
                          heldout_fail_reason=hv["fail_reason"])
            if solved and not h_solved:
                # Named, because this is the row the whole second metric exists
                # for: runnable code that reproduces every example it was shown
                # and encodes the wrong rule.
                record["status"] = "solved_shown_only"
        record["sample_idx"] = gen.get("sample_idx", 0)
        results.append(record)

    scored = [r for r in results if "solved" in r]
    n = len(scored)
    report = {
        "n_generations": len(generations),
        "n_scored": n,
        "n_format_valid": n_parsed,
        "n_solved": n_solved,
        "n_truncated_format_failures": n_truncated,
        "n_unparseable_code": n_unparseable,
        "n_prompt_truncated": n_prompt_cut,
        "solve_rate": n_solved / n if n else 0.0,
        "format_valid_rate": n_parsed / n if n else 0.0,
        "results": results,
    }

    # The held-out block. Present only when the corpus supplied unseen pairs;
    # absent-with-a-caveat otherwise, so a reader can never mistake a run that
    # could not measure the rule for one that measured it at 0.
    report["n_heldout_scored"] = n_heldout_avail
    if n_heldout_avail:
        report["n_heldout_solved"] = n_heldout_solved
        report["heldout_solve_rate"] = n_heldout_solved / n_heldout_avail
        report["solve_rate_on_heldout_subset"] = (
            n_shown_solved_in_subset / n_heldout_avail)
        report["n_solved_shown_only"] = n_shown_pass_heldout_fail
        report["overfit_gap"] = (report["solve_rate_on_heldout_subset"]
                                 - report["heldout_solve_rate"])
        report["heldout_note"] = (
            f"heldout_solve_rate ({report['heldout_solve_rate']:.3f}) is the ARC "
            f"question: the code is right on {n_heldout_solved}/{n_heldout_avail} "
            f"inputs no generator saw. solve_rate over those same rows is "
            f"{report['solve_rate_on_heldout_subset']:.3f}; the "
            f"{report['overfit_gap']:+.3f} gap is {n_shown_pass_heldout_fail} "
            f"programs that reproduce every shown example and encode the wrong "
            f"rule. Compare runs on heldout_solve_rate")
    else:
        report["heldout_caveat"] = (
            "no val row carried heldout_pairs, so solve_rate is measured against "
            "the pairs each prompt already showed the model -- it says the student "
            "writes runnable programs, NOT that it found the rule. On real ARC "
            "roughly 1 in 11 shown-pair-verified solvers is a wrong program. "
            "Rebuild the corpus through build_heldout_source.py to get the "
            "measurement this number is routinely mistaken for")
    report.update(aggregate_samples(scored))

    # The same rate over rows that saw their whole task. Reported ALONGSIDE
    # solve_rate rather than instead of it: dropping the cut rows silently would
    # inflate the headline, and reporting only the blended figure hides that some
    # rows were never given the question. Both, plus the count, lets a reader see
    # the size of the gap instead of taking a number on trust.
    n_intact = n - n_prompt_cut
    if n_prompt_cut:
        report["solve_rate_intact_prompts"] = (
            (n_solved - n_prompt_cut_solved) / n_intact if n_intact else 0.0)
        report["n_scored_intact_prompts"] = n_intact
        # The window is read off the generations rather than hardcoded: it is a
        # generator flag now, and a caveat naming a stale number is worse than one
        # naming none -- it invites sizing decisions against a window nobody used.
        windows = {g.get("input_window") for g in generations if g.get("input_window")}
        window = f"{windows.pop()}-token" if len(windows) == 1 else ""
        report["prompt_caveat"] = (
            f"{n_prompt_cut}/{n} prompts exceeded the generator's {window} input "
            f"window and lost their oldest context to left-truncation. Those rows "
            f"were scored on an incomplete task description, so their failures are a "
            f"data gap rather than model ability -- including any that parsed and "
            f"then failed verification, which look like wrong programs but are wrong "
            f"inputs. solve_rate_intact_prompts "
            f"({report['solve_rate_intact_prompts']:.3f} over {n_intact} rows) is the "
            f"figure to compare across runs; widen the window to remove the caveat "
            f"instead of reading around it")
    if n_truncated:
        report["format_caveat"] = (
            f"{n_truncated} of the {n - n_parsed} format failures ran out of "
            f"generation tokens mid-answer, so format_valid_rate "
            f"({report['format_valid_rate']:.3f}) partly measures the token budget, "
            f"not the model; raise --max-new-tokens before reading it as ability")
    return report


def key_fields(key):
    return {"task_id": key[0], "variant": key[1]}


def select_attempt(group: list[dict]) -> dict:
    """Choose the attempt a submission would send, using only what it may see.

    ARC allows k attempts per task, and the choice has to be made without the
    answer. The only free signal is the pairs the prompt already showed, so the
    lowest-numbered attempt that reproduces them wins, falling back to the lowest-
    numbered attempt when none does.

    Deliberately NOT "any attempt that is held-out-correct": that is the oracle,
    and a rate computed from it measures whether the model could have been right
    rather than whether the submission would have been. Both are reported below --
    the oracle as `pass_at_k`, this as `selected_at_k` -- and the gap between them
    is how much a better selector could buy.
    """
    ordered = sorted(group, key=lambda r: r.get("sample_idx", 0))
    for r in ordered:
        if r.get("solved"):
            return r
    return ordered[0]


def aggregate_samples(scored: list[dict]) -> dict:
    """Group k attempts per task into per-task rates. Empty when k == 1.

    Without this, k attempts per task enter `solve_rate` as k independent rows: the
    headline becomes a per-attempt average while the run reports having made k
    attempts per task, and the two get read as the same number. pass@2 is strictly
    higher than the per-attempt rate, so the confusion always flatters.
    """
    groups: dict[tuple, list[dict]] = {}
    for r in scored:
        groups.setdefault((r["task_id"], r["variant"]), []).append(r)
    sizes = {len(g) for g in groups.values()}
    if not groups or sizes == {1}:
        return {}

    k = max(sizes)
    out: dict = {"n_tasks": len(groups), "samples_per_task": k}
    if len(sizes) > 1:
        # A ragged run is usually an interrupted one. Reporting pass@k over it
        # without saying so credits the tasks that got more attempts.
        out["sampling_caveat"] = (
            f"attempts per task range {min(sizes)}-{k}; tasks with fewer attempts "
            f"had fewer chances, so pass@{k} here is a mixture, not a pass@{k}")

    shown_any = sum(any(r.get("solved") for r in g) for g in groups.values())
    out["shown_pass_at_k"] = shown_any / len(groups)

    with_heldout = [g for g in groups.values()
                    if any("heldout_solved" in r for r in g)]
    if with_heldout:
        oracle = sum(any(r.get("heldout_solved") for r in g) for g in with_heldout)
        selected = sum(bool(select_attempt(g).get("heldout_solved"))
                       for g in with_heldout)
        out["n_tasks_with_heldout"] = len(with_heldout)
        out["pass_at_k"] = oracle / len(with_heldout)
        out["selected_at_k"] = selected / len(with_heldout)
        out["selection_loss"] = out["pass_at_k"] - out["selected_at_k"]
        out["sampling_note"] = (
            f"{k} attempts per task. selected_at_k ({out['selected_at_k']:.3f}) is "
            f"what a submission scores, choosing by shown-pair self-verification; "
            f"pass_at_k ({out['pass_at_k']:.3f}) is the oracle upper bound. The "
            f"{out['selection_loss']:.3f} gap is tasks solved by SOME attempt that "
            f"a real selector would not have sent. solve_rate and heldout_solve_rate "
            f"above are per-ATTEMPT rates over {len(scored)} generations, not "
            f"per-task -- do not read them as pass@{k}")
    else:
        out["sampling_note"] = (
            f"{k} attempts per task, but no row carried unseen pairs, so only "
            f"shown_pass_at_k could be computed -- and selecting by the same "
            f"shown-pair signal it measures makes it an oracle over a tautology. "
            f"solve_rate above is a per-ATTEMPT rate, not pass@{k}")
    return out


PRIMARY_METRIC = "heldout_solve_rate"
FALLBACK_METRIC = "solve_rate"
# Best first. Each answers a strictly harder question than the next: what a
# submission of k attempts would actually score; whether one attempt got the unseen
# input right; whether one attempt ran and reproduced the examples it was shown.
METRIC_CHAIN = ["selected_at_k", PRIMARY_METRIC, FALLBACK_METRIC]

METRIC_LABELS = {
    "selected_at_k": "correctness on unseen inputs as a submission would score it",
    PRIMARY_METRIC: "correctness on unseen inputs",
    FALLBACK_METRIC: "executable correctness on shown examples",
}


def sampling_mismatch(*reports: dict | None) -> bool:
    """True when the sides of a comparison made different numbers of attempts.

    pass@2 exceeds pass@1 for the same model, so comparing a 2-attempt run against
    a 1-attempt one attributes the extra attempt to the fine-tuning.
    """
    return len({(r.get("samples_per_task") or 1) for r in reports if r}) > 1


def pick_metric(*reports: dict | None) -> str:
    """The hardest question EVERY side of the comparison can answer.

    All-or-nothing on purpose: comparing a student's held-out rate against a
    baseline's shown-pair rate would subtract the overfit tax from one side only
    and report the difference as an effect. Per-task pass@k is skipped when the
    runs used different k, which is the same failure one level up.
    """
    present = [r for r in reports if r]
    if not present:
        return FALLBACK_METRIC
    for metric in METRIC_CHAIN:
        if metric == "selected_at_k" and sampling_mismatch(*present):
            continue
        if all(r.get(metric) is not None for r in present):
            return metric
    return FALLBACK_METRIC


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

    Held-out pairs do not cure this: a corpus gated on them contains only tasks
    whose teacher solver was held-out-correct, so the teacher scores ~1.0 on
    `heldout_solve_rate` too, by the same construction. The gate switches to the
    honest metric (`pick_metric`) because the STUDENT's number then means
    something; the baseline is still selection-bound either way.
    """
    degenerate_above = 0.99
    metric = pick_metric(student, teacher)
    s = student[metric]
    gate = {"gate_ratio": ratio, "metric": metric, "student_rate": s,
            "student_solve_rate": student["solve_rate"]}
    if not teacher:
        gate.update(status="NO_TEACHER_BASELINE", passed=None)
        return gate

    t = teacher[metric]
    threshold = ratio * t
    gate.update(teacher_rate=t, teacher_solve_rate=teacher["solve_rate"],
                threshold=threshold, passed=s >= threshold)
    gate["status"] = "PASSED" if gate["passed"] else "FAILED"

    if t >= degenerate_above:
        gate["baseline_caveat"] = (
            f"teacher {metric} is {t:.3f}; on a val set built from verified "
            f"solutions this is expected by construction, so the gate is really an "
            f"absolute {threshold:.1%} bar, not a {ratio:.0%} comparison")
    elif t == 0.0:
        gate["baseline_caveat"] = (
            f"teacher {metric} is 0.000, so any student passes arithmetically; "
            f"the gate carries no quality signal")
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
    # The gain is computed on whichever metric BOTH sides can support, and
    # `primary_metric` says which -- an unlabelled "absolute_gain" that silently
    # switches between two different questions is worse than either one alone.
    metric = pick_metric(student, base)
    s, b = student[metric], base[metric]
    lift = {
        "primary_metric": metric,
        "base_rate": b,
        "student_rate": s,
        "base_solve_rate": base["solve_rate"],
        "student_solve_rate": student["solve_rate"],
        "absolute_gain": s - b,
        "relative_gain": (s - b) / b if b else None,
        "base_format_valid_rate": base.get("format_valid_rate"),
        "student_format_valid_rate": student.get("format_valid_rate"),
    }
    if metric != FALLBACK_METRIC:
        # Both shown-pair rates too, so a run where the student learned to
        # reproduce examples without learning the rule is visible rather than
        # hidden behind a single flat number.
        lift["shown_pair_gain"] = student["solve_rate"] - base["solve_rate"]
        lift["base_overfit_gap"] = base.get("overfit_gap")
        lift["student_overfit_gap"] = student.get("overfit_gap")
    if sampling_mismatch(student, base):
        # Named even though pick_metric already refused the per-task metric: the
        # per-attempt rates being compared are still drawn from runs with different
        # attempt budgets, which changes nothing arithmetically and everything about
        # how the pair of runs was set up.
        lift["sampling_caveat"] = (
            f"attempts per task differ ({base.get('samples_per_task') or 1} base vs "
            f"{student.get('samples_per_task') or 1} student), so per-task pass@k was "
            f"not compared; the two runs were not configured alike")
    label = METRIC_LABELS[metric]
    if s > b:
        lift["verdict"] = f"fine-tuning improved {label}"
    elif s == b:
        lift["verdict"] = f"no change in {label}"
    else:
        lift["verdict"] = f"REGRESSION — fine-tuning made {label} worse"

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

    When the corpus carries held-out pairs the oracle must score 1.000 on THOSE
    too, and that is a stronger check than it looks: the corpus was gated on the
    base solver, but each variant's held-out pairs were pushed through the same
    group element g as its prompt by augment.py. A wrong g there -- inverted,
    applied to the input only, off by a rotation -- produces a corpus whose own
    verified code fails its own held-out pairs. This is the end-to-end assertion
    that the transform chain is sound.
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

    n_h = rep["n_heldout_scored"]
    if n_h:
        print(f"oracle held-out: {rep['n_heldout_solved']}/{n_h} "
              f"(heldout_solve_rate {rep['heldout_solve_rate']:.3f})")
        for bad in (r for r in rep["results"] if r.get("heldout_solved") is False):
            print(f"  KNOWN-GOOD FAILS ITS OWN HELD-OUT PAIR {bad['task_id']} "
                  f"{bad['variant']}: {str(bad.get('heldout_fail_reason'))[:160]}")
        if rep["heldout_solve_rate"] != 1.0:
            ok = False
    else:
        print("oracle held-out: not measured — no val row carries heldout_pairs")

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
    print(f"  shown pairs  : {report['n_solved']} ({report['solve_rate']:.1%})")
    if report["n_heldout_scored"]:
        print(f"  HELD-OUT     : {report['n_heldout_solved']}"
              f"/{report['n_heldout_scored']} ({report['heldout_solve_rate']:.1%})"
              f"  <- the ARC question")
        print(f"  overfit gap  : {report['overfit_gap']:+.1%} "
              f"({report['n_solved_shown_only']} programs right on every shown "
              f"example, wrong rule)")
    else:
        print(f"  HELD-OUT     : not measured — {report['heldout_caveat']}")
    if report.get("format_caveat"):
        print(f"  TRUNCATED    : {report['format_caveat']}")
    print(f"  gate         : {report['gate']['status']}")
    if report["gate"].get("baseline_caveat"):
        print(f"  CAVEAT       : {report['gate']['baseline_caveat']}")
    if report["lift"]:
        lift = report["lift"]
        print(f"  vs base      : {lift['base_rate']:.1%} -> {lift['student_rate']:.1%} "
              f"({lift['absolute_gain']:+.1%}) on {lift['primary_metric']}")
        print(f"  lift verdict : {lift['verdict']}")
    else:
        print("  vs base      : not measured (pass --base-report for distillation lift)")
    return 0 if report["gate"].get("passed") is not False else 1


if __name__ == "__main__":
    sys.exit(main())
