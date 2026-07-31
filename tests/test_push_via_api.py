"""Unit tests for tools/push_via_api.py — pure parsing/planning, no network, no git.

The push path has no staging environment: a wrong tree entry lands on the PR branch
and the damage is invisible in the diff (a lost executable bit renders as "0
insertions, 0 deletions"). These tests are the only place the planning logic gets
checked before it writes to a real branch.

Run: .venv/bin/python -m pytest tests/test_push_via_api.py -q
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


push = _load("push_via_api", "tools/push_via_api.py")


def raw(*records: str) -> bytes:
    """Build a `git diff --raw -z` payload from ':meta\\0path' style strings."""
    return b"".join(r.encode() + b"\0" for r in records)


def plan(payload: bytes) -> list[dict]:
    return push.tree_entries(push.parse_raw_diff(payload), lambda p: f"blob-of-{p}")


# --- the defect that shipped: mode came from a constant, not from git -----------

def test_an_executable_file_stays_executable():
    entries = plan(raw(":100755 100755 aaa bbb M", "deploy/console/deploy.sh"))
    assert entries == [{"path": "deploy/console/deploy.sh", "mode": "100755",
                       "type": "blob", "sha": "blob-of-deploy/console/deploy.sh"}]


def test_a_chmod_with_no_content_change_is_still_pushed():
    """git shows identical blob shas for a mode-only change; the entry must survive.

    This is exactly the repair case for a branch that already lost the bit -- if
    planning dropped entries whose content did not change, the fix could never be
    pushed.
    """
    entries = plan(raw(":100644 100755 aaa aaa M", "hooks/pre-commit"))
    assert [e["mode"] for e in entries] == ["100755"]


def test_a_symlink_keeps_its_own_mode():
    entries = plan(raw(":120000 120000 aaa bbb M", "link"))
    assert entries[0]["mode"] == "120000"


# --- statuses the old one-line parser could not express ------------------------

def test_a_deletion_removes_the_path_instead_of_uploading_it():
    uploaded = []
    ops = push.parse_raw_diff(raw(":100644 000000 aaa 000 D", "gone.py"))
    entries = push.tree_entries(ops, lambda p: uploaded.append(p) or "x")
    assert entries == [{"path": "gone.py", "mode": "100644", "type": "blob", "sha": None}]
    assert uploaded == [], "a deleted path has no content to upload"


def test_a_rename_deletes_the_old_path_and_adds_the_new_one():
    entries = plan(raw(":100644 100644 aaa aaa R100", "old/a.py", "new/a.py"))
    assert [(e["path"], e["sha"]) for e in entries] == [
        ("old/a.py", None), ("new/a.py", "blob-of-new/a.py")]


def test_a_copy_adds_the_new_path_and_keeps_the_old_one():
    entries = plan(raw(":100644 100644 aaa aaa C100", "src.py", "copy.py"))
    assert [e["path"] for e in entries] == ["copy.py"], \
        "a copy leaves the source in place; deleting it would lose a file"


def test_an_added_file_is_uploaded():
    entries = plan(raw(":000000 100644 000 bbb A", "new.py"))
    assert entries == [{"path": "new.py", "mode": "100644", "type": "blob",
                       "sha": "blob-of-new.py"}]


def test_several_changes_keep_their_order_and_their_own_modes():
    entries = plan(raw(
        ":100644 100644 aaa bbb M", "a.py",
        ":100755 100755 ccc ddd M", "b.sh",
        ":100644 000000 eee 000 D", "c.py",
    ))
    assert [(e["path"], e["mode"], e["sha"] is None) for e in entries] == [
        ("a.py", "100644", False), ("b.sh", "100755", False), ("c.py", "100644", True)]


# --- paths that break naive splitting -----------------------------------------

def test_a_path_with_a_space_survives():
    ops = push.parse_raw_diff(raw(":100644 100644 aaa bbb M", "docs/my notes.md"))
    assert ops[0]["path"] == "docs/my notes.md"


def test_a_non_ascii_path_survives():
    ops = push.parse_raw_diff(raw(":100644 100644 aaa bbb M", "docs/說明.md"))
    assert ops[0]["path"] == "docs/說明.md"


def test_an_empty_diff_plans_nothing():
    assert push.parse_raw_diff(b"") == []
    assert plan(b"") == []


# --- refusals: better to stop than to push a broken tree ----------------------

def test_a_submodule_bump_is_refused_rather_than_pushed_as_a_blob():
    with pytest.raises(ValueError, match="submodule"):
        plan(raw(":160000 160000 aaa bbb M", "vendor/dep"))


def test_a_truncated_diff_is_refused():
    with pytest.raises(ValueError, match="truncated"):
        push.parse_raw_diff(b":100644 100644 aaa bbb M\0")


def test_a_rename_missing_its_second_path_is_refused():
    with pytest.raises(ValueError, match="truncated"):
        push.parse_raw_diff(raw(":100644 100644 aaa aaa R100", "only/one/path"))


def test_a_malformed_meta_field_is_refused():
    with pytest.raises(ValueError, match="malformed"):
        push.parse_raw_diff(raw(":100644 100644 aaa M", "a.py"))


def test_a_diff_that_does_not_start_with_a_meta_field_is_refused():
    with pytest.raises(ValueError, match="meta field"):
        push.parse_raw_diff(raw("a.py"))


# --- the repo's own executables are the regression this protects ---------------

def test_every_tracked_executable_in_this_repo_would_push_as_executable():
    """A mode-preservation guard anchored to the real files, not to a fixture.

    If someone adds a script and the pusher regresses to a constant mode, this
    fails naming the file that would silently stop being runnable.
    """
    import subprocess
    listing = subprocess.run(["git", "ls-files", "-s"], cwd=REPO,
                             capture_output=True, text=True, check=True).stdout
    execs = [line.split("\t", 1)[1] for line in listing.splitlines()
             if line.startswith("100755")]
    assert execs, "expected this repo to track at least one executable"
    for path in execs:
        entries = plan(raw(":100755 100755 aaa bbb M", path))
        assert entries[0]["mode"] == "100755", f"{path} would lose its executable bit"
