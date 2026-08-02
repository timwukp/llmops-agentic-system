"""Tests for `verify_code`, the single arbiter of whether a task counts as solved.

Why this file exists as its own suite: `verify_code` had no direct test — it was
only ever exercised through `eval_student.score_generations`, and a defect in the
verdict it returns is therefore invisible from any test that reads a solve rate,
because the solve rate is computed FROM that verdict. The empty-pairs case below
is exactly that: `all_pass` came back True for any code at all, so every rate
derived from it was inflated and every test agreed with itself.

The sandbox mechanics (timeouts, import guarding, escape attempts) are covered
where they are used, in tests/test_eval_student.py. This file pins the verdict
contract.

Run: .venv/bin/python -m pytest tests/test_verify_sandbox.py -q
"""
from __future__ import annotations

import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pipeline/v2"))

from verify_sandbox import verify_code  # noqa: E402

DOUBLE = "def transform(grid):\n    return [[c * 2 for c in row] for row in grid]\n"
PAIRS = [{"input": [[1, 2]], "output": [[2, 4]]},
         {"input": [[3]], "output": [[6]]}]


def test_code_reproducing_every_pair_passes():
    v = verify_code(DOUBLE, PAIRS)
    assert v["all_pass"] is True
    assert (v["n_pass"], v["n_pairs"]) == (2, 2)
    assert v["fail_reason"] is None


def test_an_empty_pair_list_refuses_instead_of_passing_vacuously():
    """`n_pass == len(pairs)` is TRUE for zero pairs, so without this guard ANY code
    on a task whose pairs failed to parse read as solved — inflating every solve rate
    computed from it, silently, in the direction that looks like success.

    An unverifiable task must not read as a solved one. Refusing here fixes it at the
    single source of truth rather than at each call site, so a future caller cannot
    reintroduce it by forgetting the check.
    """
    v = verify_code(DOUBLE, [])
    assert v["all_pass"] is False
    assert (v["n_pass"], v["n_pairs"]) == (0, 0)
    assert v["fail_reason"] == "no pairs to verify against"


def test_the_refusal_holds_for_obviously_wrong_code_too():
    """The guard must not depend on the code being plausible — the point is that
    nothing was checked, whatever was submitted."""
    assert verify_code("def transform(g):\n    return [[9]]\n", [])["all_pass"] is False
    assert verify_code("", [])["all_pass"] is False


def test_stops_at_the_first_failing_pair_and_names_it():
    """Which pair failed is the difference between "wrong idea" and "wrong edge case",
    and the count of passing pairs is how a reviewer tells those apart."""
    v = verify_code(DOUBLE, [PAIRS[0], {"input": [[1]], "output": [[99]]}])
    assert v["all_pass"] is False
    assert v["n_pass"] == 1
    assert "pair 1" in v["fail_reason"] and "mismatch" in v["fail_reason"]


def test_partial_credit_is_not_a_pass():
    v = verify_code("def transform(g):\n    return [[1, 2]]\n", PAIRS)
    assert v["n_pass"] == 0 and v["all_pass"] is False


def test_a_crash_is_a_failure_not_an_exception_to_the_caller():
    """One bad generation must not take down a scoring run over hundreds of rows."""
    v = verify_code("def transform(g):\n    raise RuntimeError('boom')\n", PAIRS)
    assert v["all_pass"] is False
    assert "pair 0" in v["fail_reason"] and "boom" in v["fail_reason"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
