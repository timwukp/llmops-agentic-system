"""Tests for pipeline/v2/build_eval120_val.py, which builds the MAIN METRIC's corpus.

Until this module existed the only thing in the suite that touched the builder was
`test_augment.py`'s hardcoded-home-directory scan -- which is how a default pointing at one
developer's home directory sat in `main()` unnoticed. (Spelled out in words rather than
written as a path, because that scan reads every tracked file including this one, and a
literal example here is indistinguishable to a regex from the defect it describes. It caught
this docstring on the first run.) A 400-line module whose output IS the number the whole
experiment reports had no external test, and its internal `self_test()` cannot supply one:
a self-test proves the renderer is self-consistent, not that the suite would go red if it
stopped being.

So the two things tested here are the two ways this file can silently produce a plausible
wrong answer:

  * the corpus is built from the WRONG ARC FILES. This reads the held-out evaluation set,
    and a path default that happens to resolve on one machine makes "which 120 tasks did we
    measure" unanswerable after the fact.
  * an eval row carries a TRAINING row's provenance. `verified` / `heldout_ok` /
    `repair_rounds` describe a distilled solver, and there is no solver on an eval row; a
    `verified: true` there would be a lie with a schema, and the scorer would believe it.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "build_eval120_val", REPO / "pipeline/v2/build_eval120_val.py")
bev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bev)


# --------------------------------------------------------------------------------
# fixtures: a two-task ARC evaluation set, in the real file shape
# --------------------------------------------------------------------------------

def _challenges():
    return {
        "aaaa1111": {"train": [{"input": [[1, 2]], "output": [[2, 1]]},
                               {"input": [[3]], "output": [[3]]}],
                     "test": [{"input": [[7, 8]]}]},
        "bbbb2222": {"train": [{"input": [[0]], "output": [[5]]}],
                     # Two test inputs: 120-task ARC-AGI-2 has tasks with more than
                     # one, and a builder that assumed one would drop the second
                     # silently -- the row would still look complete.
                     "test": [{"input": [[1]]}, {"input": [[2]]}]},
    }


def _solutions():
    return {"aaaa1111": [[[8, 7]]],
            "bbbb2222": [[[6]], [[9]]]}


def _arc_dir(tmp_path, challenges=None, solutions=None):
    d = tmp_path / "arc"
    d.mkdir()
    (d / "arc-agi_evaluation_challenges.json").write_text(
        json.dumps(_challenges() if challenges is None else challenges))
    (d / "arc-agi_evaluation_solutions.json").write_text(
        json.dumps(_solutions() if solutions is None else solutions))
    return d


def _run(argv, env=None, monkeypatch=None):
    """Call main() with argv and a controlled environment; return (rc, stdout, stderr)."""
    monkeypatch.setattr(sys, "argv", ["build_eval120_val.py"] + argv)
    monkeypatch.delenv(bev.ARC_EVAL_DIR_ENV, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    return bev.main()


# --------------------------------------------------------------------------------
# where the corpus comes from
# --------------------------------------------------------------------------------

def test_no_arc_directory_is_an_error_naming_both_the_flag_and_the_variable(
        tmp_path, monkeypatch, capsys):
    """Exit 2 rather than 1, and say how to fix it two ways.

    Asserting only "nonzero" would pass for the renderer self-test failing, which is a
    completely different problem with a completely different fix.
    """
    rc = _run(["--out", str(tmp_path / "o.jsonl")], monkeypatch=monkeypatch)
    err = capsys.readouterr().err
    assert rc == 2, f"expected the usage-style exit 2, got {rc}"
    assert "--arc-dir" in err and bev.ARC_EVAL_DIR_ENV in err, \
        f"the error names neither the flag nor the env var: {err!r}"


def test_the_env_var_resolves_both_evaluation_files(tmp_path, monkeypatch, capsys):
    out = tmp_path / "o.jsonl"
    rc = _run(["--out", str(out)], env={bev.ARC_EVAL_DIR_ENV: str(_arc_dir(tmp_path))},
              monkeypatch=monkeypatch)
    capsys.readouterr()
    assert rc == 0
    rows = [json.loads(l) for l in out.read_text().splitlines()]
    assert len(rows) == 2, f"expected one row per task, got {len(rows)}"


def test_the_env_var_is_read_at_call_time_not_import_time(tmp_path, monkeypatch, capsys):
    """A module-level `os.environ.get` would bake in whatever was set when pytest
    imported this file, so the flag would appear to work while actually being frozen.
    Set the variable AFTER import (which already happened at the top of this module)
    and it must still take effect."""
    out = tmp_path / "o.jsonl"
    monkeypatch.setenv(bev.ARC_EVAL_DIR_ENV, str(_arc_dir(tmp_path)))
    monkeypatch.setattr(sys, "argv", ["build_eval120_val.py", "--out", str(out)])
    assert bev.main() == 0
    capsys.readouterr()
    assert out.exists()


def test_an_explicit_challenges_path_overrides_the_directory(tmp_path, monkeypatch,
                                                             capsys):
    """--challenges must win over $V2_ARC_EVAL_DIR, and it must win ALONE: --solutions
    still resolves from the directory. A resolver that required both or neither would
    make "point at one modified file" impossible."""
    d = _arc_dir(tmp_path)
    one = {"cccc3333": {"train": [{"input": [[1]], "output": [[2]]}],
                        "test": [{"input": [[4]]}]}}
    alt = tmp_path / "alt_challenges.json"
    alt.write_text(json.dumps(one))
    (d / "arc-agi_evaluation_solutions.json").write_text(
        json.dumps({"cccc3333": [[[9]]]}))
    out = tmp_path / "o.jsonl"
    rc = _run(["--out", str(out), "--challenges", str(alt)],
              env={bev.ARC_EVAL_DIR_ENV: str(d)}, monkeypatch=monkeypatch)
    capsys.readouterr()
    assert rc == 0
    rows = [json.loads(l) for l in out.read_text().splitlines()]
    assert [r["task_id"] for r in rows] == ["cccc3333"], \
        "the explicit --challenges file was not the one read"


def test_a_corpus_check_without_a_training_file_or_a_directory_is_an_error(
        tmp_path, monkeypatch, capsys):
    corpus = tmp_path / "c.jsonl"
    corpus.write_text("")
    d = _arc_dir(tmp_path)
    # Both eval files given explicitly, so the ONLY thing missing is the training file
    # the corpus check needs. Without this isolation the test would pass on the
    # already-tested "no arc dir at all" path and prove nothing about --corpus.
    rc = _run(["--out", str(tmp_path / "o.jsonl"),
               "--challenges", str(d / "arc-agi_evaluation_challenges.json"),
               "--solutions", str(d / "arc-agi_evaluation_solutions.json"),
               "--corpus", str(corpus)], monkeypatch=monkeypatch)
    assert rc == 2
    assert "--train-challenges" in capsys.readouterr().err


def test_no_tracked_default_points_at_a_developer_machine():
    """The defect this module was written after. Kept local to the builder as well as
    in the repo-wide scan, because the repo-wide one lives in test_augment.py and a
    future narrowing of ITS file list would silently stop covering this path."""
    text = (REPO / "pipeline/v2/build_eval120_val.py").read_text()
    # These two needles are bare prefixes with no path segment after the final slash, which
    # is the only reason the repo-wide scan in test_augment.py does not flag this line: its
    # regex requires at least one character from [A-Za-z0-9._-] to follow. Do not "improve"
    # them into example paths -- that turns this guard into an offender.
    for needle in ("/Users/", "/home/"):
        assert needle not in text, \
            f"{needle!r} is back in the builder; take the path from --arc-dir or " \
            f"${bev.ARC_EVAL_DIR_ENV}"


# --------------------------------------------------------------------------------
# what an eval row may and may not claim
# --------------------------------------------------------------------------------

def test_an_eval_row_carries_no_solver_provenance():
    """`verified` / `heldout_ok` / `repair_rounds` are properties of a DISTILLED SOLVER.
    There is no solver on an eval row, so the keys must be absent rather than False:
    absent means "not applicable", False means "we checked and it failed", and the
    scorer's held-out branch reads them."""
    rows = bev.build_rows(_challenges(), _solutions(), "a")
    for r in rows:
        for k in ("verified", "heldout_ok", "repair_rounds", "code"):
            assert k not in r, \
                f"eval row {r['task_id']} claims {k}={r[k]!r}, which no eval row can know"


def test_the_held_out_pairs_are_the_real_solutions_in_order():
    """Both the count and the VALUES, and for the two-test task specifically. Zipping
    test inputs against solutions is only correct if the orders agree; a builder that
    paired input[0] with solution[1] would produce a corpus where every model looks
    wrong, and nothing downstream could tell that from a hard task."""
    rows = {r["task_id"]: r for r in bev.build_rows(_challenges(), _solutions(), "a")}
    assert rows["bbbb2222"]["n_test_inputs"] == 2
    assert rows["bbbb2222"]["heldout_pairs"] == [
        {"input": [[1]], "output": [[6]]},
        {"input": [[2]], "output": [[9]]},
    ]
    assert rows["aaaa1111"]["heldout_pairs"] == [{"input": [[7, 8]], "output": [[8, 7]]}]


def test_the_test_input_never_appears_in_the_prompt():
    """The whole experiment is invalid if it does. Checked on the rendered prompt for
    every fixture task rather than on the renderer's own self-test, because the row is
    what gets shipped and `render_prompt` is only one of the things that builds it."""
    for r in bev.build_rows(_challenges(), _solutions(), "a"):
        for pair in r["heldout_pairs"]:
            grid = bev.render_grid_block(pair["input"]).strip()
            assert grid not in r["prompt"], \
                f"task {r['task_id']}: the held-out input is IN the prompt"
            assert bev.render_grid_block(pair["output"]).strip() not in r["prompt"]


def test_the_split_label_marks_these_rows_as_the_held_out_eval_set():
    rows = bev.build_rows(_challenges(), _solutions(), "b")
    assert {r["split"] for r in rows} == {"arc2_eval120"}
    assert {r["template"] for r in rows} == {"b"}, \
        "the row does not record which template rendered it, so a base-vs-finetuned " \
        "comparison cannot prove the two runs used the same one"


def test_a_task_with_no_solution_is_not_silently_dropped():
    """A missing solutions entry means the corpus is incomplete, and 119 rows scored as
    if they were 120 moves the headline number. Whatever it does, it must not return a
    short list and exit 0."""
    ch = _challenges()
    sol = _solutions()
    del sol["bbbb2222"]
    try:
        rows = bev.build_rows(ch, sol, "a")
    except (KeyError, SystemExit, ValueError):
        return
    assert len(rows) == len(ch), \
        f"{len(ch)} tasks in, {len(rows)} rows out, and no error raised"


# --------------------------------------------------------------------------------
# the self-test is only worth its runtime if the suite reds when the renderer breaks
# --------------------------------------------------------------------------------

def test_the_renderer_self_test_passes_on_the_shipped_renderer():
    st = bev.self_test()
    failed = [k for k, v in st.items() if not v]
    assert not failed, f"self_test failures: {failed}"
    assert len(st) >= 15, f"only {len(st)} self-test assertions; the set has shrunk"


@pytest.mark.parametrize("wrong", [
    pytest.param(lambda g: f"({len(g)}x{len(g[0])}):\n"
                           + "\n".join(",".join(str(c) for c in r) for r in g) + "\n",
                 id="comma_separator"),
    pytest.param(lambda g: f"({len(g[0])}x{len(g)}):\n"
                           + "\n".join(" ".join(str(c) for c in r) for r in g) + "\n",
                 id="transposed_dims"),
    pytest.param(lambda g: "\n".join(" ".join(str(c) for c in r) for r in g) + "\n",
                 id="no_dim_header"),
])
def test_a_broken_renderer_makes_the_self_test_fail(monkeypatch, wrong):
    """The external half of the self-test's own claim. `self_test()` contains
    `detects_*` mutation checks, but those mutate the EXPECTED string while calling the
    real renderer -- so they prove the comparison is sensitive, not that a broken
    renderer is caught. This breaks the renderer itself and demands failures."""
    monkeypatch.setattr(bev, "render_grid_block", wrong)
    st = bev.self_test()
    assert [k for k, v in st.items() if not v], \
        "render_grid_block was replaced with a wrong implementation and self_test " \
        "still reported everything passing"


def test_main_refuses_to_write_a_corpus_when_the_self_test_fails(
        tmp_path, monkeypatch, capsys):
    """A renderer that drifted one byte from the training text is the v1 failure mode,
    and the cost of shipping it is a mysteriously worse solve rate rather than an error.
    So the file must not exist afterwards -- exiting nonzero while leaving a corpus on
    disk invites the next step to read it."""
    out = tmp_path / "o.jsonl"
    monkeypatch.setattr(bev, "self_test", lambda: {"a_ends_on_grid": False})
    rc = _run(["--out", str(out)], env={bev.ARC_EVAL_DIR_ENV: str(_arc_dir(tmp_path))},
              monkeypatch=monkeypatch)
    assert rc == 1, f"expected 1 for a renderer failure, got {rc}"
    assert "self-test FAILED" in capsys.readouterr().err
    assert not out.exists(), "a corpus was written despite the renderer self-test failing"


# --------------------------------------------------------------------------------
# the two-template finding is a measurement, so its numbers must be derivable
# --------------------------------------------------------------------------------

def test_the_template_shares_are_a_partition_of_the_corpus():
    """79.6% + 20.4% = 1.0 and 677 + 172 = 849. Both halves matter: the shares could
    sum to 1 with the task counts wrong, and a docstring citing 79.6% while the table
    says something else is the version of this claim that goes stale."""
    total_share = sum(t["share_of_train_rows"] for t in bev.TEMPLATES.values())
    assert abs(total_share - 1.0) < 0.001, f"shares sum to {total_share}, not 1.0"
    assert sum(t["source_tasks"] for t in bev.TEMPLATES.values()) == 849
    doc = bev.__doc__ or ""
    for t in bev.TEMPLATES.values():
        pct = f"{t['share_of_train_rows'] * 100:.1f}%"
        assert pct in doc, f"the module docstring does not state {pct}"
        assert str(t["source_tasks"]) in doc


def test_only_template_b_asks_for_code_only():
    """The flag's help text calls B "the one that asks for code-only output", and that
    is the whole reason the choice is not arbitrary. Derived from the template text
    rather than trusted from the boolean beside it."""
    for name, t in bev.TEMPLATES.items():
        says = "Output ONLY the function." in (t["head"] + t["tail"])
        assert says == t["asks_for_code_only"], \
            f"template {name}: asks_for_code_only={t['asks_for_code_only']} but the " \
            f"text {'does' if says else 'does not'} ask for code only"
    assert [n for n, t in bev.TEMPLATES.items() if t["asks_for_code_only"]] == ["b"]


def test_the_two_templates_are_not_the_same_prompt(monkeypatch):
    """Guards against the merge that makes `--template` a no-op: base and fine-tuned
    would then agree perfectly and the flag would look validated."""
    pairs = [{"input": [[1]], "output": [[2]]}]
    a, b = bev.render_prompt(pairs, "a"), bev.render_prompt(pairs, "b")
    assert a != b
    assert len(set(bev.TEMPLATES[k]["head"] for k in bev.TEMPLATES)) == 2, \
        "the two templates share a head, so --template selects nothing at the start " \
        "of the prompt where the model is most sensitive to it"
