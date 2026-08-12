"""Tests for the v2 augmentation engine's inputs, and a repo-wide guard against the
defect they close.

`augment.py` named its corpus with two absolute paths, one of them under a single
laptop's home directory (`/Users/<name>/Downloads/kaggle-arc-agi-2/...`, a sibling
checkout of a DIFFERENT project). Nothing invoked the module, so nothing failed --
which is exactly why it survived: a path that only resolves on one machine is
indistinguishable from a working one until somebody else runs it, and by then the
module is a training-data generator producing a plausible corpus for whatever it
happened to find.

So this file pins two things: that the source is refused rather than guessed, and
that no tracked code file anywhere in the repo hardcodes a home directory again.

Run: .venv/bin/python -m pytest tests/test_augment.py -q
"""
from __future__ import annotations

import json
import os
import pathlib
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


def test_the_worker_payload_carries_the_arc_dir_rather_than_inheriting_it():
    """Under `spawn` the module is re-imported in a fresh interpreter, so a global
    set by main() in the parent reverts to its default in every worker. The tuple
    process_task unpacks is therefore part of the contract."""
    src = (REPO / "pipeline/v2/augment.py").read_text()
    assert "row, n_variants, arc_dir = args" in src, \
        "process_task no longer unpacks arc_dir from its argument tuple"
    assert "tasks = [(r, args.n_variants, arc_dir) for r in rows]" in src, \
        "main() no longer puts arc_dir in the worker payload"


def test_load_source_rows_reads_the_path_it_is_given(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("".join(json.dumps({"task_id": f"t{i}"}) + "\n" for i in range(5)))
    assert [r["task_id"] for r in augment.load_source_rows(str(p))] == \
        ["t0", "t1", "t2", "t3", "t4"]
    assert len(augment.load_source_rows(str(p), limit=2)) == 2


# ── repo-wide: no tracked code names somebody's home directory ────────────────

# Prose may legitimately quote a path from an incident; code that RUNS may not.
_CODE_SUFFIXES = (".py", ".sh", ".json", ".yml", ".yaml", ".js")
_HOME_PREFIXES = ("/Users/", "/home/")


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
            if any(pref in line for pref in _HOME_PREFIXES):
                offenders.append(f"{rel}:{i}: {line.strip()[:100]}")
    assert not offenders, (
        "code that runs must take machine-specific paths as arguments or "
        "environment, not hardcode one developer's home directory:\n"
        + "\n".join(offenders))


def test_the_scan_would_catch_the_defect_it_was_written_for(tmp_path):
    """The guard above is only worth its runtime if it fires on the original line.
    Checked against the real string that shipped, not a paraphrase of it."""
    original = ('SOURCE_JSONL = ("/Users/tmwu/Downloads/kaggle-arc-agi-2/v2-design/'
                'prototype/"')
    assert any(pref in original for pref in _HOME_PREFIXES), \
        "the prefixes no longer match the line this guard exists to catch"
    assert not any(pref in "SOURCE_ENV = \"V2_SOURCE_JSONL\"" for pref in _HOME_PREFIXES)


def test_the_source_env_name_is_documented_where_a_runner_looks():
    """A required input nobody can discover is a broken script with extra steps."""
    doc = (REPO / "pipeline/v2/README.md").read_text()
    src = (REPO / "pipeline/v2/augment.py").read_text()
    assert augment.SOURCE_ENV in src.split('"""')[1], \
        "the module docstring must name the env var that supplies the corpus"
    assert augment.SOURCE_ENV in doc or "--source" in doc, \
        "pipeline/v2/README.md documents the augment invocation; it must name the input"
