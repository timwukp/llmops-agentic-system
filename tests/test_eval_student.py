"""Unit tests for the v2 eval scorer — no GPU, no AWS.

An eval that can be fooled is worse than no eval: it manufactures confidence.
So these tests attack the scorer from both sides. The oracle direction (real
verified solutions must score 1.0) is covered against live data by
`--self-test`; here we pin the adversarial direction — every way a wrong or
sneaky generation could be mistaken for a solve.

Run: .venv/bin/python -m pytest tests/test_eval_student.py -q
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pipeline/v2"))

spec = importlib.util.spec_from_file_location("eval_student", REPO / "pipeline/v2/eval_student.py")
es = importlib.util.module_from_spec(spec)
spec.loader.exec_module(es)


# A minimal task: output = input with every cell doubled, two train pairs.
PROMPT = """Solve this ARC task. Write a Python function `transform(grid)` that implements the transformation.

Training pair 1:
Input (2x2):
1 2
3 4
Output (2x2):
2 4
6 8

Training pair 2:
Input (1x3):
0 1 2
Output (1x3):
0 2 4
"""

VAL = [{"task_id": "t1", "variant": "orig", "prompt": PROMPT, "n_train_pairs": 2}]

CORRECT = "def transform(grid):\n    return [[c * 2 for c in row] for row in grid]\n"


def gen(text, task_id="t1", variant="orig"):
    return [{"task_id": task_id, "variant": variant, "generation": text}]


def score(text, **kw):
    return es.score_generations(gen(text, **kw), VAL)


# ---------------------------------------------------------------- prompt parsing

def test_parse_pairs_recovers_grids_and_shapes():
    pairs = es.parse_pairs(PROMPT)
    assert len(pairs) == 2
    assert pairs[0] == {"input": [[1, 2], [3, 4]], "output": [[2, 4], [6, 8]]}
    assert pairs[1] == {"input": [[0, 1, 2]], "output": [[0, 2, 4]]}


def test_parse_pairs_rejects_shape_that_contradicts_its_header():
    """A declared 2x2 with three rows means the prompt is corrupt; scoring it
    would silently compare against the wrong ground truth."""
    with pytest.raises(ValueError, match="declares 2x2"):
        es.parse_pairs(PROMPT.replace("Input (2x2):\n1 2\n3 4", "Input (2x2):\n1 2\n3 4\n5 6"))


def test_parse_pairs_handles_the_second_header_variant():
    """225 of the 1000 val rows use the longer header; both must parse."""
    alt = PROMPT.replace(
        "that implements the transformation.",
        "that converts input grids to output grids.\n"
        "Grid = list of lists of int (0-9). Return a list of lists of int.")
    assert len(es.parse_pairs(alt)) == 2


# ---------------------------------------------------------------- code extraction

@pytest.mark.parametrize("wrapper", [
    "```python\n{c}```",
    "```\n{c}```",
    "Here is my solution:\n\n```python\n{c}```\nHope that helps!",
    "<think>Let me reason about this at length...</think>\n```python\n{c}```",
    "{c}",                                     # bare, unfenced
    "I'll solve it.\n{c}",                     # prose then bare code
    "```python\n{c}",                          # unterminated fence (truncated output)
])
def test_extract_code_survives_realistic_output_shapes(wrapper):
    assert es.score_generations(gen(wrapper.format(c=CORRECT)), VAL)["n_solved"] == 1


def test_generation_without_transform_is_a_format_failure_not_a_solve():
    for text in ["", "I don't know how to solve this.",
                 "```python\ndef helper(x):\n    return x\n```",
                 "```python\n# transform goes here\n```"]:
        rep = es.score_generations(gen(text), VAL)
        assert rep["n_solved"] == 0
        assert rep["n_format_valid"] == 0
        assert rep["results"][0]["status"] == "no_transform_emitted"


# ------------------------------------------------------- adversarial: must NOT pass

def test_wrong_answer_fails():
    rep = score("```python\ndef transform(grid):\n    return grid\n```")
    assert rep["n_solved"] == 0
    assert rep["results"][0]["status"] == "failed_verification"
    assert rep["n_format_valid"] == 1, "it parsed fine — it was simply wrong"


def test_code_that_does_not_compile_is_a_format_failure_not_a_wrong_answer():
    """`extract_code` only regex-matches a `def transform(` line, so a body that stops
    mid-expression arrives here carrying the signature and nothing runnable.

    Found by running negative controls against the real val set: a generation cut off
    at `out = [[c for c in row] for row in gr` scored format_valid **1.000** over 200
    rows. That inflates the two places the number is load-bearing — the verdict
    `compute_lift` falls back to when both solve rates are 0 (entirely expected for a
    1.7B student on ARC-AGI-2) and the pipeline's `format_validity: 0.95` gate — so a
    model that emitted 200 unparseable stubs would have read as perfectly well-formed.
    """
    for code in ["def transform(grid)\n    return grid",              # missing colon
                 "def transform(grid):\n    out = [[c for c in row",   # cut mid-expr
                 "def transform(grid):\n    return (1,"]:              # unclosed paren
        rep = score(f"```python\n{code}\n```")
        assert rep["n_format_valid"] == 0, code
        assert rep["n_unparseable_code"] == 1, code
        assert rep["results"][0]["status"] == "unparseable_code", code
        assert "SyntaxError" in rep["results"][0]["fail_reason"], code
        assert rep["n_solved"] == 0, code


def test_unparseable_code_is_distinct_from_emitting_no_code_at_all():
    """Both are format failures, but they say different things about the model: one
    tried and ran out, the other never wrote a `transform`. A single bucket would hide
    which one a run is dominated by, and only the first is fixed by more tokens."""
    nothing = score("I don't know how to solve this.")
    broken = score("```python\ndef transform(grid)\n    return grid\n```")
    assert nothing["results"][0]["status"] == "no_transform_emitted"
    assert nothing["n_unparseable_code"] == 0
    assert broken["results"][0]["status"] == "unparseable_code"
    assert broken["n_unparseable_code"] == 1
    assert nothing["n_format_valid"] == broken["n_format_valid"] == 0


def test_valid_code_that_merely_crashes_at_runtime_stays_format_valid():
    """The negative control for the compile check: it must reject only code that does
    not PARSE. A NameError is a wrong program — the model wrote real Python and got the
    answer wrong, which is exactly what `failed_verification` means. Folding it into
    the format bucket would understate format validity as badly as the original bug
    overstated it."""
    rep = score("```python\ndef transform(grid):\n    return undefined_name(grid)\n```")
    assert rep["n_format_valid"] == 1
    assert rep["n_unparseable_code"] == 0
    assert rep["results"][0]["status"] == "failed_verification"
    assert rep["n_solved"] == 0


def test_partially_correct_answer_gets_no_credit():
    """Correct on pair 1, wrong on pair 2. Exact match means all-or-nothing."""
    code = ("def transform(grid):\n"
            "    if len(grid) == 2:\n"
            "        return [[c * 2 for c in row] for row in grid]\n"
            "    return grid\n")
    rep = score(f"```python\n{code}```")
    assert rep["n_solved"] == 0
    assert rep["results"][0]["pairs_passed"] == 1
    assert rep["results"][0]["pairs_total"] == 2


def test_hardcoded_output_that_ignores_input_fails_on_the_second_pair():
    """The classic cheat: memorize pair 1's answer. Multiple pairs defeat it."""
    code = "def transform(grid):\n    return [[2, 4], [6, 8]]\n"
    rep = score(f"```python\n{code}```")
    assert rep["n_solved"] == 0


def test_crashing_code_fails_without_taking_the_scorer_down():
    rep = score("```python\ndef transform(grid):\n    raise RuntimeError('boom')\n```")
    assert rep["n_solved"] == 0
    assert "boom" in (rep["results"][0]["fail_reason"] or "")


def test_infinite_loop_is_killed_by_the_timeout():
    rep = es.score_generations(
        gen("```python\ndef transform(grid):\n    while True:\n        pass\n```"), VAL)
    assert rep["n_solved"] == 0


def test_sandbox_blocks_filesystem_escape():
    """Generated code is untrusted: it must not be able to read the disk."""
    code = ("def transform(grid):\n"
            "    import os\n"
            "    return [[len(os.listdir('/'))]]\n")
    rep = score(f"```python\n{code}```")
    assert rep["n_solved"] == 0
    assert "not allowed" in (rep["results"][0]["fail_reason"] or "").lower()


def test_generation_for_an_unknown_task_is_reported_not_silently_dropped():
    rep = es.score_generations(gen(f"```python\n{CORRECT}```", task_id="ghost"), VAL)
    assert rep["n_scored"] == 0
    assert rep["results"][0]["status"] == "no_matching_val_row"
    assert rep["solve_rate"] == 0.0


def test_variant_must_match_so_a_sibling_variant_cannot_be_credited():
    """Variants of one task share a task_id but need different code. Scoring
    against the wrong variant's pairs would be a silent leak."""
    rep = es.score_generations(gen(f"```python\n{CORRECT}```", variant="rot90"), VAL)
    assert rep["results"][0]["status"] == "no_matching_val_row"


# ---------------------------------------------------------------- the quality gate

def _rep(rate):
    return {"solve_rate": rate}


def test_gate_is_relative_to_the_teacher():
    assert es.apply_gate(_rep(0.40), _rep(0.50), 0.80)["passed"] is True   # exactly 0.80x
    assert es.apply_gate(_rep(0.41), _rep(0.50), 0.80)["passed"] is True
    assert es.apply_gate(_rep(0.39), _rep(0.50), 0.80)["passed"] is False


def test_gate_without_a_teacher_baseline_refuses_to_claim_a_pass():
    gate = es.apply_gate(_rep(0.9), None, 0.80)
    assert gate["passed"] is None
    assert gate["status"] == "NO_TEACHER_BASELINE"


def test_zero_teacher_rate_does_not_make_every_student_pass_by_accident():
    """0.80 x 0 == 0, so a 0% student meets a 0% teacher. That is arithmetically
    true and must be reported honestly rather than dressed up as quality."""
    gate = es.apply_gate(_rep(0.0), _rep(0.0), 0.80)
    assert gate["passed"] is True
    assert gate["threshold"] == 0.0
    assert "no quality signal" in gate["baseline_caveat"]


def test_perfect_teacher_baseline_is_flagged_as_degenerate():
    """This val set only contains tasks with a verified solution, so a teacher
    re-measured on it scores ~1.0 by construction and the 'relative' gate is
    really an absolute bar. The report must say so rather than imply a comparison."""
    gate = es.apply_gate(_rep(0.85), _rep(1.0), 0.80)
    assert gate["passed"] is True
    assert "by construction" in gate["baseline_caveat"]
    assert "absolute 80.0% bar" in gate["baseline_caveat"]


def test_an_informative_baseline_carries_no_caveat():
    gate = es.apply_gate(_rep(0.20), _rep(0.35), 0.80)
    assert "baseline_caveat" not in gate


# --------------------------------------------- lift vs the un-fine-tuned model

def _full(solve, fmt=1.0):
    return {"solve_rate": solve, "format_valid_rate": fmt}


def test_lift_measures_gain_over_the_same_model_before_fine_tuning():
    lift = es.compute_lift(_full(0.30), _full(0.10))
    assert lift["absolute_gain"] == pytest.approx(0.20)
    assert lift["relative_gain"] == pytest.approx(2.0)
    assert "improved" in lift["verdict"]


def test_lift_names_a_regression_rather_than_reporting_a_negative_gain_quietly():
    lift = es.compute_lift(_full(0.05), _full(0.20))
    assert lift["absolute_gain"] == pytest.approx(-0.15)
    assert lift["verdict"].startswith("REGRESSION")


def test_lift_from_a_zero_base_reports_no_relative_gain_instead_of_dividing_by_zero():
    lift = es.compute_lift(_full(0.10), _full(0.0))
    assert lift["relative_gain"] is None
    assert lift["absolute_gain"] == pytest.approx(0.10)


def test_two_zero_solve_rates_redirect_to_format_validity():
    """A 1.7B student on ARC-AGI-2 can legitimately score 0 both before and after.
    Reporting a 0.0 gain would imply the question was settled; it wasn't."""
    lift = es.compute_lift(_full(0.0, fmt=0.90), _full(0.0, fmt=0.10))
    assert "cannot distinguish" in lift["verdict"]
    assert "0.100 -> 0.900" in lift["verdict"]


def test_lift_is_none_without_a_base_report():
    assert es.compute_lift(_full(0.5), None) is None


# ----------------------------------- truncation vs inability (lift confound)

def test_a_truncated_format_failure_is_attributed_to_the_token_budget():
    """A base Qwen3 can spend its whole budget on <think> and emit no transform.
    Read naively that says "cannot write code"; it says "ran out of tokens"."""
    rep = es.score_generations(
        [{"task_id": "t1", "variant": "orig", "generation": "<think>hmm",
          "truncated": True}], VAL)
    assert rep["n_format_valid"] == 0
    assert rep["n_truncated_format_failures"] == 1
    assert rep["results"][0]["truncated"] is True
    assert "token budget" in rep["format_caveat"]
    assert "--max-new-tokens" in rep["format_caveat"]


def test_an_untruncated_format_failure_carries_no_budget_excuse():
    """The caveat must not fire for a model that simply refused to write code —
    that would explain away a real failure."""
    rep = es.score_generations(
        [{"task_id": "t1", "variant": "orig", "generation": "I don't know."}], VAL)
    assert rep["n_truncated_format_failures"] == 0
    assert "format_caveat" not in rep


def test_truncation_does_not_excuse_a_generation_that_did_emit_code():
    """Truncated but parseable code is scored on its merits, not waved through."""
    rep = es.score_generations(
        [{"task_id": "t1", "variant": "orig", "truncated": True,
          "generation": "def transform(grid):\n    return grid\n"}], VAL)
    assert rep["n_format_valid"] == 1
    assert rep["n_solved"] == 0
    assert "format_caveat" not in rep


def test_empty_generations_report_is_zero_not_a_crash():
    rep = es.score_generations([], VAL)
    assert rep == {**rep, "n_solved": 0, "solve_rate": 0.0, "n_scored": 0}


# ------------------------------------------- prompts the model never fully received

def test_a_cut_prompt_gets_a_second_rate_over_rows_that_saw_the_whole_task():
    """A row whose prompt was left-truncated lost its oldest context, so its failure
    is a data gap rather than model ability. Reported ALONGSIDE solve_rate: dropping
    those rows silently would inflate the headline, and reporting only the blended
    figure hides that some rows were never given the question."""
    rep = es.score_generations(
        [{"task_id": "t1", "variant": "orig", "generation": CORRECT},
         {"task_id": "t1", "variant": "orig", "generation": "def transform(g):\n    return g\n",
          "prompt_truncated": True}], VAL)
    assert rep["solve_rate"] == 0.5, "the blended rate must still be reported"
    assert rep["n_prompt_truncated"] == 1
    assert rep["n_scored_intact_prompts"] == 1
    assert rep["solve_rate_intact_prompts"] == 1.0
    assert "data gap" in rep["prompt_caveat"]


def test_a_cut_prompt_that_parsed_and_then_failed_is_still_counted_as_cut():
    """This is the case that looks least like a data problem: format-valid code that
    fails verification reads as a wrong program when it is a wrong input."""
    rep = es.score_generations(
        [{"task_id": "t1", "variant": "orig", "prompt_truncated": True,
          "generation": "def transform(g):\n    return g\n"}], VAL)
    assert rep["n_format_valid"] == 1 and rep["n_solved"] == 0
    assert rep["results"][0]["status"] == "failed_verification"
    assert rep["results"][0]["prompt_truncated"] is True
    assert rep["n_prompt_truncated"] == 1
    assert rep["solve_rate_intact_prompts"] == 0.0
    assert rep["n_scored_intact_prompts"] == 0


def test_no_cut_prompts_means_no_caveat_and_no_second_rate():
    """The caveat must not fire on a clean run — it would imply a gap that isn't
    there and invite readers to discount a number that needs no discount."""
    rep = score(CORRECT)
    assert rep["n_prompt_truncated"] == 0
    assert "prompt_caveat" not in rep
    assert "solve_rate_intact_prompts" not in rep


def test_a_prompt_with_no_pairs_is_excluded_rather_than_scored_either_way():
    """Nothing was verified, so scoring it as solved credits the model for a data
    defect and scoring it as failed blames the model for one. Neither is honest:
    leave it out of the denominator and name the status."""
    rep = es.score_generations(
        [{"task_id": "bare", "variant": "orig", "generation": CORRECT}],
        [{"task_id": "bare", "variant": "orig", "prompt": "Solve this task."}])
    assert rep["results"][0]["status"] == "no_pairs_in_prompt"
    assert "solved" not in rep["results"][0], "it must not enter the denominator"
    assert rep["n_scored"] == 0 and rep["n_solved"] == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
