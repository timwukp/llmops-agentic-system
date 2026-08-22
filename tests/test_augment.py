"""Tests for the v2 augmentation engine's inputs, and a repo-wide guard against the
defect they close.

`augment.py` named its corpus with two absolute paths, one of them a per-user home
directory pointing at a sibling checkout of a DIFFERENT project. Nothing invoked the
module, so nothing failed -- which is exactly why it survived: a path that only
resolves on one machine is indistinguishable from a working one until somebody else
runs it, and by then the module is a training-data generator producing a plausible
corpus for whatever it happened to find.

So this file pins two things: that the source is refused rather than guessed, and
that no tracked code file anywhere in the repo hardcodes a home directory again.

Note on how this file is written: the repo-wide guard below scans every tracked code
file, INCLUDING this one, so the literal prefixes it hunts for cannot appear here as
literals -- they are a regex, and the historical line used as the guard's positive
control is assembled from pieces. Exempting this file by path would have been the
easy way out and would have left the guard unable to see its own suite. It found this
out the hard way: the first push failed CI with three self-reports.

Run: .venv/bin/python -m pytest tests/test_augment.py -q
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pipeline/v2"))

import augment  # noqa: E402


# ── the source corpus is an input, not a default ──────────────────────────────

def test_no_source_is_refused_with_the_flag_that_supplies_it(monkeypatch):
    """Silence is the failure mode to avoid: augmenting the wrong corpus yields a
    zero-noise training set for the wrong task, which looks entirely healthy."""
    monkeypatch.delenv(augment.SOURCE_ENV, raising=False)
    with pytest.raises(SystemExit) as e:
        augment.resolve_source(None)
    msg = str(e.value)
    assert "--source" in msg and augment.SOURCE_ENV in msg, msg


def test_a_source_that_does_not_exist_is_refused_before_any_work(monkeypatch):
    monkeypatch.setenv(augment.SOURCE_ENV, "/nonexistent/triplets.jsonl")
    with pytest.raises(SystemExit) as e:
        augment.resolve_source(None)
    assert "/nonexistent/triplets.jsonl" in str(e.value)


def test_the_cli_value_wins_over_the_environment(tmp_path, monkeypatch):
    chosen = tmp_path / "chosen.jsonl"
    chosen.write_text("")
    other = tmp_path / "other.jsonl"
    other.write_text("")
    monkeypatch.setenv(augment.SOURCE_ENV, str(other))
    assert augment.resolve_source(str(chosen)) == str(chosen)
    assert augment.resolve_source(None) == str(other), \
        "with no flag the environment must still supply the corpus"


def test_the_arc_directory_is_optional_and_the_prompt_is_the_fallback():
    """--arc-dir is a speedup, not a dependency: the train pairs are recoverable
    from the rendered prompt, which is what makes a missing scratch dir harmless."""
    prompt = ("Task:\nExample 1 input (1x2):\n1 2\nExample 1 output (1x2):\n2 4\n")
    pairs = augment.load_train_pairs("nope", prompt, "/nonexistent/arc")
    assert pairs == [{"input": [[1, 2]], "output": [[2, 4]]}], pairs


def test_the_worker_payload_carries_every_per_run_knob_rather_than_inheriting_it():
    """Under `spawn` the module is re-imported in a fresh interpreter, so a global
    set by main() in the parent reverts to its default in every worker. The tuple
    process_task unpacks is therefore part of the contract -- and it is a contract
    per knob, not per file: the held-out gate arrived later and a gate that
    reverts to its default inside the workers is not a gate.

    Pinned as a set comparison rather than one literal line so that adding a knob
    to only ONE end of the payload fails here instead of at runtime in a worker.
    """
    src = (REPO / "pipeline/v2/augment.py").read_text()
    unpack = re.search(r"^\s*(.+?) = args$", src, re.M)
    payload = re.search(r"tasks = \[\((.+?)\) for r in rows\]", src)
    assert unpack and payload, "the worker payload is no longer built or unpacked"
    unpacked = [n.strip() for n in unpack.group(1).split(",")]
    sent = [n.strip() for n in payload.group(1).split(",")]
    assert unpacked[0] == "row" and sent[0] == "r"
    assert len(unpacked) == len(sent), \
        f"payload sends {len(sent)} values, worker unpacks {len(unpacked)}"
    for knob in ("arc_dir", "require_heldout"):
        assert knob in unpacked, f"process_task no longer unpacks {knob}"
        assert any(knob in s for s in sent), \
            f"main() no longer puts {knob} in the worker payload"


# ── the held-out gate: the wrapper cannot see a wrong rule, so the source must ──

# input -> every cell doubled. Two shown pairs; the held-out pair uses a value
# neither shown pair contains, which is what lets a wrong rule be caught.
DOUBLE_PROMPT = ("Solve this ARC task.\n\n"
                 "Training pair 1:\nInput (1x2):\n1 2\nOutput (1x2):\n2 4\n\n"
                 "Training pair 2:\nInput (1x1):\n3\nOutput (1x1):\n6\n")
DOUBLE_PAIRS = [{"input": [[1, 2]], "output": [[2, 4]]},
                {"input": [[3]], "output": [[6]]}]
HELDOUT = [{"input": [[4]], "output": [[8]]}]
RIGHT_RULE = "def transform(grid):\n    return [[c * 2 for c in row] for row in grid]\n"
# Reproduces both SHOWN pairs exactly and is the wrong rule: a lookup table that
# happens to cover every value it was shown. This is the shape the measured 1-in-11
# overfit solvers take -- runnable, plausible, right on the examples in the prompt.
LYING_RULE = ("def transform(grid):\n"
              "    table = {1: 2, 2: 4, 3: 6}\n"
              "    return [[table.get(c, c) for c in row] for row in grid]\n")


def _payload(code, heldout, *, n_variants=3, require_heldout=True, task_id="00576224"):
    row = {"task_id": task_id, "prompt": DOUBLE_PROMPT, "code": code}
    if heldout is not None:
        row["heldout_pairs"] = heldout
    return (row, n_variants, "/nonexistent/arc", require_heldout)


def test_a_wrong_rule_that_passes_every_shown_pair_takes_all_its_variants_with_it():
    """The gate's reason for existing. `build_wrapped_code` is correct by
    construction, so it propagates the base program's semantics faithfully --
    including a wrong rule, into all 25 variants, each of which then passes the
    sandbox. Gating per variant would be theatre; the whole task has to go."""
    res = augment.process_task(_payload(LYING_RULE, HELDOUT))
    assert res["emitted"] == [], "a wrong rule must not reach the training set"
    assert res["heldout_status"] == "gated_out"
    assert res["heldout_fail_reason"], "the reason must be recorded, not just the verdict"
    assert res["rejected"] == [], \
        "it was not rejected for failing shown pairs -- it passes those"


def test_the_shown_pair_check_alone_would_have_accepted_that_same_solver():
    """The other half of the control. Without this assertion the test above passes
    just as happily against a solver that is broken in some ordinary way, and would
    keep passing if the held-out gate were deleted and replaced by a stricter shown-
    pair check. What must be shown is that the two verdicts DISAGREE."""
    from verify_sandbox import verify_code
    assert verify_code(LYING_RULE, DOUBLE_PAIRS)["all_pass"] is True, \
        "the lying double must be indistinguishable from correct on shown pairs"
    assert verify_code(LYING_RULE, HELDOUT)["all_pass"] is False
    assert verify_code(RIGHT_RULE, DOUBLE_PAIRS)["all_pass"] is True
    assert verify_code(RIGHT_RULE, HELDOUT)["all_pass"] is True


def test_a_source_row_without_heldout_pairs_is_refused_by_default():
    """Silence is the failure to avoid: augmenting an ungated corpus produces rows
    stamped `verified: true` whose verification means only "reproduces what the
    generator was shown"."""
    res = augment.process_task(_payload(RIGHT_RULE, None))
    assert res["emitted"] == []
    assert res["heldout_status"] == "missing"


def test_opting_out_is_possible_and_explicit():
    """--allow-missing-heldout must actually work, or the only path is to fake a
    held-out pair -- which is worse than knowingly running without one."""
    res = augment.process_task(_payload(RIGHT_RULE, None, require_heldout=False))
    assert len(res["emitted"]) == 4, res["emitted"]
    assert all(r["heldout_ok"] is False for r in res["emitted"])
    assert all(r["heldout_pairs"] == [] for r in res["emitted"])


def test_every_emitted_variant_carries_the_heldout_pair_pushed_through_its_own_g():
    """Checked WITHOUT running the wrapper: the base solver is applied to the
    original held-out input and the result transformed by g, which must equal the
    emitted held-out output. An error in `transform_pairs` that also happened to
    be an error in `build_wrapped_code` would cancel out under the sandbox check
    and leave the eval scoring against a grid that is not the answer."""
    res = augment.process_task(_payload(RIGHT_RULE, HELDOUT, n_variants=24))
    assert len(res["emitted"]) == 25, len(res["emitted"])
    plans = {p["sig"]: p for p in augment.plan_variants("00576224", 24)}
    plans["orig"] = {"geom": None, "perm": None}

    for row in res["emitted"]:
        sig = row["variant"].split("#", 1)[-1] if "#" in row["variant"] else "orig"
        plan = plans[sig]
        assert len(row["heldout_pairs"]) == 1, row["variant"]
        pair = row["heldout_pairs"][0]
        expected_in = augment.transform_grid(HELDOUT[0]["input"],
                                            plan["geom"], plan["perm"])
        base_out = [[c * 2 for c in r] for r in HELDOUT[0]["input"]]
        expected_out = augment.transform_grid(base_out, plan["geom"], plan["perm"])
        assert pair["input"] == expected_in, row["variant"]
        assert pair["output"] == expected_out, row["variant"]
        assert row["heldout_ok"] is True
        assert row["repair_rounds"] == "unknown", \
            "absent provenance must be labelled, not defaulted to a number"


def test_provenance_travels_as_a_tag_and_never_as_a_filter():
    """After the gate both populations are held-out-correct by definition, so
    excluding feedback-repaired solvers would only discard the hardest tasks. The
    rounds count is kept so the two can be compared later, not filtered now."""
    row, n, arc, req = _payload(RIGHT_RULE, HELDOUT)
    res = augment.process_task(({**row, "repair_rounds": 7}, n, arc, req))
    assert len(res["emitted"]) == 4
    assert {r["repair_rounds"] for r in res["emitted"]} == {7}


def test_load_source_rows_reads_the_path_it_is_given(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("".join(json.dumps({"task_id": f"t{i}"}) + "\n" for i in range(5)))
    assert [r["task_id"] for r in augment.load_source_rows(str(p))] == \
        ["t0", "t1", "t2", "t3", "t4"]
    assert len(augment.load_source_rows(str(p), limit=2)) == 2


# ── repo-wide: no tracked code names somebody's home directory ────────────────

# Prose may legitimately quote a path from an incident; code that RUNS may not.
_CODE_SUFFIXES = (".py", ".sh", ".json", ".yml", ".yaml", ".js")
# A regex, not two string literals, so this file does not match its own guard. The
# alternative -- exempting tests/test_augment.py by path -- would blind the scan to
# every future test file, which is code that runs too.
_HOME_PATH_RE = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+")


def _tracked_code_files():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, check=True,
                         capture_output=True, text=True).stdout
    return [p for p in out.split("\0")
            if p and p.endswith(_CODE_SUFFIXES)]


def test_no_tracked_code_file_hardcodes_a_home_directory():
    files = _tracked_code_files()
    # A scan that reads nothing must not be able to pass -- the zero-file scan is
    # the failure this repo has already been bitten by once. The floor is a count
    # AND the one file that actually carried the defect: a count alone would still
    # pass if git ls-files were narrowed to some subtree that excluded it.
    assert len(files) > 50, \
        f"only {len(files)} tracked code files found; the scan did not run"
    assert "pipeline/v2/augment.py" in files, \
        "the scan no longer covers the file this guard was written for"
    offenders = []
    for rel in files:
        text = (REPO / rel).read_text(errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            hit = _HOME_PATH_RE.search(line)
            if hit:
                offenders.append(f"{rel}:{i}: {hit.group(0)}")
    assert not offenders, (
        "code that runs must take machine-specific paths as arguments or "
        "environment, not hardcode one developer's home directory:\n"
        + "\n".join(offenders))


def test_the_scan_would_catch_the_defect_it_was_written_for():
    """The guard above is only worth its runtime if it fires on the original line.
    Checked against the real string that shipped, assembled from pieces because this
    file is itself in the scan's file list."""
    original = ('SOURCE_JSONL = ("' + "/Users" + "/tmwu/Downloads/kaggle-arc-agi-2/"
                'v2-design/prototype/"')
    assert _HOME_PATH_RE.search(original), \
        "the pattern no longer matches the line this guard exists to catch"
    # A relative or env-driven path is not an offence, or the guard would fire on
    # every use of the fix.
    assert not _HOME_PATH_RE.search('SOURCE_ENV = "V2_SOURCE_JSONL"')
    assert not _HOME_PATH_RE.search('os.environ.get(SOURCE_ENV)')
    # /home and /Users alone are directories, not somebody's home: the pattern needs
    # a name after them, so a mount-point mention is not a false positive.
    assert not _HOME_PATH_RE.search("mounted under /home/ by the runner")


def test_the_source_env_name_is_documented_where_a_runner_looks():
    """A required input nobody can discover is a broken script with extra steps."""
    doc = (REPO / "pipeline/v2/README.md").read_text()
    src = (REPO / "pipeline/v2/augment.py").read_text()
    assert augment.SOURCE_ENV in src.split('"""')[1], \
        "the module docstring must name the env var that supplies the corpus"
    assert augment.SOURCE_ENV in doc or "--source" in doc, \
        "pipeline/v2/README.md documents the augment invocation; it must name the input"
