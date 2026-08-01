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


# --- the ref logic in main(), which had no test at all until it broke -----------
#
# Everything above tests planning. The 404-on-a-new-branch defect lived in main(),
# where the only coverage was running it for real against GitHub -- so the first
# push of every PR branch was the test, and its failure mode was "do the risky
# steps by hand instead". These fake out the two boundaries (HTTP and git) so the
# create-vs-advance decision is checked offline.

class FakeGH:
    """A GitHub double that records calls and 404s on refs it does not have."""

    def __init__(self, refs, commits=()):
        self.refs = dict(refs)          # "heads/<branch>" -> sha
        self.commits = set(commits)     # shas the remote can resolve
        self.calls = []                 # (method, path, data)

    def call(self, path, data=None, method=None, absent_ok=False):
        method = method or ("POST" if data is not None else "GET")
        self.calls.append((method, path, data))
        if path == "":
            return {"default_branch": "main"}
        if path.startswith("/git/ref/heads/") and method == "GET":
            name = path[len("/git/ref/"):]
            if name not in self.refs:
                if absent_ok:
                    return None
                raise SystemExit(f"GitHub 404 on GET {path}")
            return {"object": {"sha": self.refs[name]}}
        if path.startswith("/git/commits/") and method == "GET":
            sha = path.rsplit("/", 1)[1]
            if sha not in self.commits and sha not in self.refs.values():
                if absent_ok:
                    return None
                raise SystemExit(f"GitHub 404 on GET {path}")
            return {"tree": {"sha": f"tree-of-{sha}"}}
        if path == "/git/blobs":
            return {"sha": "blob-sha"}
        if path == "/git/trees":
            return {"sha": "local-tree"}
        if path == "/git/commits":
            return {"sha": "new-commit"}
        if path == "/git/refs" and method == "POST":
            self.refs[data["ref"][len("refs/"):]] = data["sha"]
            return {"ref": data["ref"]}
        if path.startswith("/git/refs/heads/") and method == "PATCH":
            self.refs[path[len("/git/refs/"):]] = data["sha"]
            return {}
        raise AssertionError(f"unexpected call {method} {path}")


def _run_main(monkeypatch, gh, *, branch, parent="parentsha", commit_count="7",
              ops=(":100644 100644 aaa bbb M", "pipeline/contracts/report.py")):
    """Drive main() with fake git + fake HTTP; return (exit code, gh)."""
    def fake_git(*args, binary=False):
        if args[0] == "config":
            return "https://github.com/acme/llmops.git"
        if args[:2] == ("rev-parse", "HEAD"):
            return "headsha"
        if args[:2] == ("rev-parse", "HEAD^"):
            return parent
        if args[0] == "rev-parse" and args[1].endswith("^{tree}"):
            return "local-tree"
        if args[:2] == ("rev-list", "--count"):
            return commit_count
        if args[0] == "diff":
            return raw(*ops) if binary else ""
        if args[0] == "show":
            return b"content" if binary else "content"
        if args[0] == "log":
            return "a commit message"
        raise AssertionError(f"unexpected git {args}")

    monkeypatch.setattr(push, "git", fake_git)
    monkeypatch.setattr(push, "GitHub", lambda repo, token: gh)
    # Both subprocess users in main(): `gh auth token` and the cat-file/fetch checks.
    # returncode 0 == "the base commit is already local", so no fetch is attempted.
    monkeypatch.setattr(push.subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0, "stdout": "tok\n"})())
    return push.main(["--branch", branch]), gh


def test_a_branch_that_does_not_exist_yet_is_created_not_404ed(monkeypatch):
    """The defect: a first push died on the ref 404 instead of creating the branch.

    Before the fix this raised SystemExit("GitHub 404 ..."), which is how new PR
    branches ended up being conjured by hand -- reintroducing the hand-rolled ref
    surgery this tool exists to keep humans out of.
    """
    gh = FakeGH({"heads/main": "mainsha"}, commits=["parentsha"])
    rc, gh = _run_main(monkeypatch, gh, branch="fix/brand-new")
    assert rc == 0
    assert gh.refs["heads/fix/brand-new"] == "new-commit"
    creates = [c for c in gh.calls if c[1] == "/git/refs" and c[0] == "POST"]
    assert creates and creates[0][2]["ref"] == "refs/heads/fix/brand-new"
    assert not [c for c in gh.calls if c[0] == "PATCH"], (
        "PATCH on a ref that does not exist is the 404 we just fixed")


def test_creating_a_branch_parents_it_on_the_commit_it_was_cut_from(monkeypatch):
    """The new branch's parent is HEAD^ when the remote has it -- not the default
    branch -- so the history reads as a child of where the work actually started."""
    gh = FakeGH({"heads/main": "mainsha"}, commits=["parentsha"])
    _run_main(monkeypatch, gh, branch="fix/new")
    made = [c for c in gh.calls if c[1] == "/git/commits" and c[0] == "POST"][0]
    assert made[2]["parents"] == ["parentsha"]


def test_creating_a_branch_falls_back_to_the_default_branch_head(monkeypatch):
    """When HEAD's parent is a local-only commit the remote cannot resolve, basing
    on it would 404 mid-push. Fall back to the default branch instead."""
    gh = FakeGH({"heads/main": "mainsha"})  # parentsha is NOT on the remote
    rc, gh = _run_main(monkeypatch, gh, branch="fix/new")
    assert rc == 0
    made = [c for c in gh.calls if c[1] == "/git/commits" and c[0] == "POST"][0]
    assert made[2]["parents"] == ["mainsha"]


def test_an_existing_branch_is_still_advanced_by_patch_not_recreated(monkeypatch):
    """The pre-existing path must be untouched: creating a ref that already exists
    is a 422, so a regression here would break every follow-up push on a PR."""
    gh = FakeGH({"heads/main": "mainsha", "heads/fix/old": "oldsha"},
                commits=["oldsha", "parentsha"])
    rc, gh = _run_main(monkeypatch, gh, branch="fix/old")
    assert rc == 0
    patches = [c for c in gh.calls if c[0] == "PATCH"]
    assert patches and patches[0][1] == "/git/refs/heads/fix/old"
    assert not [c for c in gh.calls if c[1] == "/git/refs" and c[0] == "POST"]
    # And it diffs against the branch's own head, not HEAD^ or the default branch.
    made = [c for c in gh.calls if c[1] == "/git/commits" and c[0] == "POST"][0]
    assert made[2]["parents"] == ["oldsha"]


def test_a_404_on_a_write_is_still_fatal(monkeypatch):
    """absent_ok is scoped to the ref lookup. A 404 anywhere else is a real failure
    and must not be softened into None, or the push proceeds on a missing object."""
    import urllib.error

    gh = push.GitHub("acme/llmops", "tok")

    def boom(req, *a, **k):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(push.urllib.request, "urlopen", boom)
    with pytest.raises(SystemExit, match="404"):
        gh.call("/git/blobs", {"content": "x"})
    # ...and the same call with absent_ok returns None rather than exiting.
    assert gh.call("/git/ref/heads/nope", absent_ok=True) is None


def test_a_first_push_still_verifies_the_tree_matches_local_head(monkeypatch):
    """Creating a branch must not skip the parity check that catches a mangled
    tree; a wrong base on a create would otherwise land silently."""
    gh = FakeGH({"heads/main": "mainsha"}, commits=["parentsha"])

    def fake_git(*args, binary=False):
        if args[0] == "config":
            return "https://github.com/acme/llmops.git"
        if args[:2] == ("rev-parse", "HEAD"):
            return "headsha"
        if args[:2] == ("rev-parse", "HEAD^"):
            return "parentsha"
        if args[0] == "rev-parse" and args[1].endswith("^{tree}"):
            return "a-different-tree"  # remote tree will be "local-tree"
        if args[:2] == ("rev-list", "--count"):
            return "7"
        if args[0] == "diff":
            return raw(":100644 100644 aaa bbb M", "a.py") if binary else ""
        if args[0] == "show":
            return b"content" if binary else "content"
        if args[0] == "log":
            return "msg"
        raise AssertionError(f"unexpected git {args}")

    monkeypatch.setattr(push, "git", fake_git)
    monkeypatch.setattr(push, "GitHub", lambda repo, token: gh)
    monkeypatch.setattr(push.subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0, "stdout": "tok\n"})())
    assert push.main(["--branch", "fix/new"]) == 1, (
        "a tree that does not match local HEAD must be reported, not called success")
