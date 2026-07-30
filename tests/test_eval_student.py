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


def test_empty_generations_report_is_zero_not_a_crash():
    rep = es.score_generations([], VAL)
    assert rep == {**rep, "n_solved": 0, "solve_rate": 0.0, "n_scored": 0}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
