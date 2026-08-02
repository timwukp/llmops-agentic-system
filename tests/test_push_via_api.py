"""Unit tests for tools/push_via_api.py — pure parsing/planning, no network, no git.

The push path has no staging environment: a wrong tree entry lands on the PR branch
and the damage is invisible in the diff (a lost executable bit renders as "0
insertions, 0 deletions"). These tests are the only place the planning logic gets
checked before it writes to a real branch.

Run: .venv/bin/python -m pytest tests/test_push_via_api.py -q
"""
from __future__ import annotations

import base64
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
    """A GitHub double that records calls and 404s on refs it does not have.

    Models GitHub's eventual consistency on ref reads via `stale_reads`: the first N
    GETs of a ref that DOES exist answer 404, exactly as the real API does for seconds
    after a branch is created. Without this the double is more consistent than GitHub,
    and the bug that cost a commit on 2026-08-01 cannot be reproduced offline.
    """

    def __init__(self, refs, commits=(), stale_reads=0, chain=()):
        self.refs = dict(refs)          # "heads/<branch>" -> sha
        self.commits = set(commits)     # shas the remote can resolve
        self.calls = []                 # (method, path, data)
        self.stale_reads = stale_reads  # ref GETs to answer 404 despite the ref existing
        self.slept = []                 # backoff delays, so a retry loop is observable
        self.created_commits = []       # every POST /git/commits payload, in order
        # The remote's existing commit chain, newest first: [(sha, tree, parent), ...].
        # Needed because an API-replayed commit has a sha the local repo has never seen,
        # so "has the remote already got this commit" can only be answered by TREE --
        # and a double that reports no parents cannot model a chain to walk at all.
        self.chain = {sha: {"tree": tree, "parent": parent}
                      for sha, tree, parent in chain}
        self.commits |= set(self.chain)

    # read_ref lives on the real GitHub class, so the double must provide it too.
    # Delegating keeps the RETRY POLICY under test instead of reimplemented here — a
    # double with its own retry loop would pass no matter what the tool does. Bound at
    # class-definition time because _run_main monkeypatches push.GitHub with a lambda,
    # so looking it up through the module at call time finds that lambda, not the class.
    #
    # `attempts` is NOT restated here on purpose. The first draft signed this
    # `read_ref(self, branch, attempts=4, sleep=None)`, which pinned the retry count in
    # the double: dropping the tool's own default to attempts=1 -- i.e. reinstating the
    # believe-the-first-404 bug that cost a commit -- kept all 28 tests green, because
    # the double supplied the 4 the tool had stopped asking for. Only **kwargs the caller
    # actually passes; let the tool's default be the tool's.
    # (Verified by patching the default to 1, 2026-08-01.)
    _real_read_ref = push.GitHub.read_ref

    def read_ref(self, branch, **kw):
        kw.setdefault("sleep", self.slept.append)
        return FakeGH._real_read_ref(self, branch, **kw)

    def call(self, path, data=None, method=None, absent_ok=False, conflict_ok=False):
        method = method or ("POST" if data is not None else "GET")
        self.calls.append((method, path, data))
        if path == "":
            return {"default_branch": "main"}
        if path.startswith("/git/ref/heads/") and method == "GET":
            name = path[len("/git/ref/"):]
            if name in self.refs and self.stale_reads > 0:
                self.stale_reads -= 1
                if absent_ok:
                    return None
                raise SystemExit(f"GitHub 404 on GET {path}")
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
            if sha in self.chain:
                node = self.chain[sha]
                return {"tree": {"sha": node["tree"]},
                        "parents": ([{"sha": node["parent"]}] if node["parent"] else [])}
            return {"tree": {"sha": f"tree-of-{sha}"}}
        if path == "/git/blobs":
            # Keyed by content so a test can prove which commit a blob was read from:
            # replaying commit 1 with HEAD's content is a silent lie about what commit 1
            # did, and a constant sha here would hide it.
            return {"sha": f"blob-of-{data['content'][:24]}"}
        if path == "/git/trees":
            # Every tree answers "local-tree" so the parity check at the end of main()
            # is satisfied; the check itself has its own test with its own double.
            return {"sha": "local-tree"}
        if path == "/git/commits":
            # Distinct shas, because a replay of N commits must CHAIN: commit 2's parent
            # is commit 1's sha, and a constant here would make a tool that parented
            # everything on the base look correct. The first keeps the name the
            # single-commit tests already assert on.
            sha = "new-commit" if not self.created_commits \
                else f"new-commit-{len(self.created_commits) + 1}"
            self.created_commits.append({"sha": sha, **data})
            self.commits.add(sha)
            return {"sha": sha}
        if path == "/git/refs" and method == "POST":
            name = data["ref"][len("refs/"):]
            if name in self.refs:
                # The real API's answer, and the one that saved a commit on 2026-08-01:
                # creating an existing ref is a 422, not an overwrite.
                if conflict_ok:
                    return None
                raise SystemExit(f"GitHub 422 on POST {path}: Reference already exists")
            self.refs[name] = data["sha"]
            return {"ref": data["ref"]}
        if path.startswith("/git/refs/heads/") and method == "PATCH":
            self.refs[path[len("/git/refs/"):]] = data["sha"]
            return {}
        raise AssertionError(f"unexpected call {method} {path}")


def _run_main(monkeypatch, gh, *, branch, parent="parentsha", commit_count="7",
              ops=(":100644 100644 aaa bbb M", "pipeline/contracts/report.py"),
              revs=None, messages=None, extra_argv=(), trees=None, parents=None):
    """Drive main() with fake git + fake HTTP; return (exit code, gh).

    `revs` is the local commit list the tool would replay (oldest first, as
    `rev-list --reverse` returns it); it defaults to a single commit, "headsha".
    `messages` maps a rev to its full message, so a test can prove each remote commit
    carries its OWN message rather than HEAD's.
    `parents` maps a rev to its FULL local parent list, so a test can model a merge. The
    default is one parent per rev, which is what every non-merge test means to describe --
    a fake that reported two would make a tool that ignores merge parents look correct.
    """
    revs = list(revs) if revs is not None else ["headsha"]
    messages = messages or {}
    parents = parents or {}
    # Per-rev tree shas, so a test can model "the remote already has THIS commit's tree".
    # Default: every rev reports "local-tree", which keeps the parity check satisfied for
    # the tests that do not care about per-commit trees.
    trees = trees or {}

    def fake_git(*args, binary=False):
        if args[0] == "config":
            return "https://github.com/acme/llmops.git"
        if args[:2] == ("rev-parse", "HEAD"):
            return "headsha"
        if args[:2] == ("rev-parse", "HEAD^"):
            return parent
        if args[0] == "rev-parse" and args[1].endswith("^{tree}"):
            return trees.get(args[1][:-len("^{tree}")], "local-tree")
        if args[:2] == ("rev-list", "--count"):
            return commit_count
        if args[:2] == ("rev-list", "--parents"):
            # `rev-list --parents -1 <rev>` -> "<rev> <parent1> [<parent2> ...]"
            rev = args[3]
            return " ".join([rev, *parents.get(rev, [f"local-parent-of-{rev}"])])
        if args[:2] == ("rev-list", "--reverse"):
            # --first-parent must be PASSED, not assumed: without it a merge replays the
            # merged branch's commits as this branch's own. Asserting on the flag here is
            # what makes the fix for defect 7 visible to a test at all.
            assert "--first-parent" in args, (
                f"rev-list built the replay range without --first-parent: {args}")
            return " ".join(revs)
        if args[0] == "diff":
            return raw(*ops) if binary else ""
        if args[0] == "show":
            # "<rev>:<path>" -- content is per-rev so a test can prove the blob comes
            # from the commit being replayed, not from HEAD.
            rev = args[1].split(":", 1)[0]
            return f"content-of-{rev}".encode() if binary else f"content-of-{rev}"
        if args[0] == "log":
            # ("log", "-1", "--format=%B"|"%s"[, rev])
            rev = args[3] if len(args) > 3 else "headsha"
            msg = messages.get(rev, f"a commit message for {rev}")
            return msg.splitlines()[0] if args[2] == "--format=%s" else msg
        raise AssertionError(f"unexpected git {args}")

    monkeypatch.setattr(push, "git", fake_git)
    monkeypatch.setattr(push, "GitHub", lambda repo, token: gh)
    # Both subprocess users in main(): `gh auth token` and the cat-file/fetch checks.
    # returncode 0 == "the base commit is already local", so no fetch is attempted.
    monkeypatch.setattr(push.subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0, "stdout": "tok\n"})())
    return push.main(["--branch", branch, *extra_argv]), gh


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
        if args[:2] == ("rev-list", "--parents"):
            return f"{args[3]} local-parent"
        if args[:2] == ("rev-list", "--reverse"):
            return "headsha"
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


# ── the eventually-consistent ref read ────────────────────────────────────────
# Found live on 2026-08-01 replaying three commits onto a fresh branch. Push 1 created
# the branch. Push 2's ref GET still 404'd (GitHub's ref reads lag by seconds), so the
# tool concluded the branch did not exist, based its commit on main, and tried to CREATE
# the ref -- discarding push 1. It survived only because POST /git/refs answered 422.
# The content was saved by the tree-parity check at the end; the HISTORY was not: push
# 2's commit message never reached the remote.

def test_a_stale_404_on_the_ref_does_not_turn_an_advance_into_a_create(monkeypatch):
    """The bug. The branch exists and the first ref read 404s anyway.

    Believing that 404 means basing the commit on the default branch, which drops every
    commit already pushed. The retry must find the ref and take the advance path.
    """
    gh = FakeGH({"heads/main": "mainsha", "heads/feat/x": "remotesha"},
                commits=["parentsha", "remotesha"], stale_reads=1)
    rc, gh = _run_main(monkeypatch, gh, branch="feat/x")
    assert rc == 0
    assert gh.refs["heads/feat/x"] == "new-commit"
    # advanced, not created
    assert [c for c in gh.calls if c[0] == "PATCH"], "must PATCH the existing ref"
    assert not [c for c in gh.calls if c[1] == "/git/refs" and c[0] == "POST"], \
        "a stale 404 must not lead to a ref CREATE on a branch that exists"
    # ...and the commit is parented on the REMOTE head, not on main
    made = [c for c in gh.calls if c[1] == "/git/commits" and c[0] == "POST"][0]
    assert made[2]["parents"] == ["remotesha"], made[2]


def test_the_ref_read_backs_off_between_attempts(monkeypatch):
    """A tight retry loop would hammer the API and still lose the race. Exponential,
    and observable, so "it retried" is not taken on faith."""
    gh = FakeGH({"heads/main": "mainsha", "heads/feat/x": "remotesha"},
                commits=["remotesha"], stale_reads=2)
    rc, gh = _run_main(monkeypatch, gh, branch="feat/x")
    assert rc == 0
    assert gh.slept == [1, 2], gh.slept


def test_a_genuinely_new_branch_is_still_created_after_the_retries(monkeypatch):
    """The retry must not turn a real 404 into a failure: a first push is the normal
    case, and it pays a few seconds of backoff rather than losing the create path."""
    gh = FakeGH({"heads/main": "mainsha"}, commits=["parentsha"])
    rc, gh = _run_main(monkeypatch, gh, branch="fix/brand-new")
    assert rc == 0
    assert gh.refs["heads/fix/brand-new"] == "new-commit"
    assert len(gh.slept) == 3, "all attempts used before concluding the branch is new"


def test_losing_the_create_race_refuses_rather_than_overwriting_the_branch(monkeypatch):
    """The last line of defence: the ref read says absent for EVERY attempt, so the
    commit gets built on the wrong base, and only then does the create come back 422.

    Falling back to PATCH here would point the branch at a commit parented on main --
    silently orphaning every commit already on it. That is the one outcome worse than
    failing, so this must exit non-zero and move nothing.
    """
    gh = FakeGH({"heads/main": "mainsha", "heads/feat/x": "remotesha"},
                commits=["parentsha", "remotesha"], stale_reads=99)
    # SystemExit, matching how every other unrecoverable API disagreement in this tool
    # reports itself; the message must name the re-run, since re-running is the fix.
    with pytest.raises(SystemExit, match="Re-run this command"):
        _run_main(monkeypatch, gh, branch="feat/x")
    assert gh.refs["heads/feat/x"] == "remotesha", "the branch must not have moved"
    assert not [c for c in gh.calls if c[0] == "PATCH"], \
        "PATCHing here would orphan the commits already on the branch"


def test_conflict_ok_is_confined_to_the_ref_create(monkeypatch):
    """A 422 anywhere else (an unprocessable tree, a bad commit) is a real failure. If
    conflict_ok leaked onto those calls it would turn corruption into silent success."""
    src = (pathlib.Path(push.__file__)).read_text()
    assert src.count("conflict_ok=True") == 1, "exactly one caller may swallow a 422"
    create = src[src.index('gh.call("/git/refs"'):]
    assert "conflict_ok=True" in create[:300]


# ── one remote commit per local commit ────────────────────────────────────────
# Found live on 2026-08-01. Two local commits went up as ONE remote commit (`3b62181`)
# carrying only HEAD's message; the rationale for the first change -- the measurement
# that justified it -- was simply absent from the remote. The tool reported "tree matches
# local HEAD exactly" and was telling the truth: the final tree is identical whether you
# replay the commits or squash them, so tree parity cannot see this. That is why these
# tests assert on the COMMIT CHAIN, not on the tree.

def test_two_local_commits_land_as_two_remote_commits_with_their_own_messages(
        monkeypatch):
    """The defect, stated as a test: N local commits must not collapse into one.

    Squashing loses every message but the last. On this repo each commit body carries
    the measurement that justified the change, so the loss is of evidence a reviewer
    needs, not of formatting.
    """
    gh = FakeGH({"heads/main": "mainsha", "heads/feat/x": "remotesha"},
                commits=["parentsha", "remotesha"])
    rc, gh = _run_main(monkeypatch, gh, branch="feat/x",
                       revs=["c1sha", "c2sha"],
                       messages={"c1sha": "First change\n\nmeasured 21.7s of 29.6s.",
                                 "c2sha": "Second change\n\nfound in the live line."})
    assert rc == 0
    assert len(gh.created_commits) == 2, (
        f"two local commits must produce two remote commits, not "
        f"{len(gh.created_commits)}: squashing discards the earlier message")
    msgs = [c["message"] for c in gh.created_commits]
    assert msgs[0].startswith("First change"), (
        f"the first remote commit must carry the FIRST local message: {msgs!r}")
    assert "measured 21.7s of 29.6s." in msgs[0], (
        "the body is where the justifying measurement lives; a subject-only replay "
        "loses exactly the part worth keeping")
    assert msgs[1].startswith("Second change"), f"messages out of order: {msgs!r}"


def test_the_replayed_commits_chain_instead_of_all_parenting_on_the_base(monkeypatch):
    """Each replayed commit's parent must be the previous one.

    Parenting all of them on the remote base would create N sibling commits and leave
    the branch pointing at one, silently dropping the others -- the same class of loss
    as the squash, arrived at from the opposite direction.
    """
    gh = FakeGH({"heads/main": "mainsha", "heads/feat/x": "remotesha"},
                commits=["parentsha", "remotesha"])
    rc, gh = _run_main(monkeypatch, gh, branch="feat/x", revs=["c1sha", "c2sha", "c3sha"])
    assert rc == 0
    # The double must hand out DISTINCT shas or this test cannot tell a chain from a
    # fan: with one constant sha, parenting everything on the base yields the same
    # `parents` list as a real chain, and a control that made the double constant passed
    # every assertion below. The apparatus requirement is asserted, not assumed.
    made = [c["sha"] for c in gh.created_commits]
    assert len(set(made)) == len(made) == 3, (
        f"the double must give each created commit its own sha, or a chain and a fan "
        f"are indistinguishable here: {made!r}")
    parents = [c["parents"] for c in gh.created_commits]
    assert parents[0] == ["remotesha"], (
        f"the first replayed commit sits on the remote head: {parents!r}")
    assert parents[1] == [gh.created_commits[0]["sha"]], (
        f"commit 2 must be a child of commit 1, not a sibling: {parents!r}")
    assert parents[2] == [gh.created_commits[1]["sha"]], (
        f"commit 3 must be a child of commit 2: {parents!r}")
    # And the branch must end up on the LAST one, not the first.
    assert gh.refs["heads/feat/x"] == gh.created_commits[-1]["sha"], (
        "the branch must point at the tip of the replayed chain")


def test_each_replayed_commit_carries_its_own_content_not_heads(monkeypatch):
    """A file changed twice must show its INTERMEDIATE content in the intermediate
    commit. Reading blobs from HEAD makes commit 1 claim a change it did not make --
    the history then reads as if the final state existed from the start, which is worse
    than a squash because it looks like real history and is not."""
    gh = FakeGH({"heads/main": "mainsha", "heads/feat/x": "remotesha"},
                commits=["parentsha", "remotesha"])
    rc, gh = _run_main(monkeypatch, gh, branch="feat/x", revs=["c1sha", "c2sha"])
    assert rc == 0
    # FakeGH keys blob shas by content, and fake_git returns "content-of-<rev>".
    blob_shas = [c[2]["content"] for c in gh.calls
                 if c[1] == "/git/blobs" and c[0] == "POST"]
    decoded = [base64.b64decode(b).decode() for b in blob_shas]
    assert decoded == ["content-of-c1sha", "content-of-c2sha"], (
        f"each commit's blob must be read from that commit: {decoded!r}")


def test_an_explicit_message_still_squashes_deliberately(monkeypatch):
    """--message is the caller SAYING "one commit, this text". Replaying N commits and
    stamping the same override on each would be worse than the squash it replaces."""
    gh = FakeGH({"heads/main": "mainsha", "heads/feat/x": "remotesha"},
                commits=["parentsha", "remotesha"])
    rc, gh = _run_main(monkeypatch, gh, branch="feat/x", revs=["c1sha", "c2sha"],
                       extra_argv=["--message", "one deliberate commit"])
    assert rc == 0
    assert len(gh.created_commits) == 1, (
        "an explicit --message asks for a single commit; N commits with one message "
        "repeated is not what the caller requested")
    assert gh.created_commits[0]["message"] == "one deliberate commit"


def test_the_plan_names_every_commit_it_will_replay(monkeypatch, capsys):
    """A dry run that prints only the file list cannot show a squash. The live squash
    went unnoticed precisely because the output looked identical either way."""
    gh = FakeGH({"heads/main": "mainsha", "heads/feat/x": "remotesha"},
                commits=["parentsha", "remotesha"])
    _run_main(monkeypatch, gh, branch="feat/x", revs=["c1sha", "c2sha"],
              messages={"c1sha": "First change\n\nbody", "c2sha": "Second change\n\nbody"},
              extra_argv=["--dry-run"])
    out = capsys.readouterr().out
    assert "2 commits" in out, f"the plan must say how many commits it replays: {out!r}"
    assert "First change" in out and "Second change" in out, (
        f"each commit's subject must be visible before anything is pushed: {out!r}")
    assert not gh.created_commits, "--dry-run must not create commits"


def test_a_rebased_branch_replays_onto_the_named_base_not_the_stale_remote_head(
        monkeypatch):
    """After a local rebase the remote head is no longer an ancestor of HEAD.

    Basing on it makes the tool replay everything between the two -- including the
    merge commits it just rebased onto -- as if they were this branch's own work. The
    plan for the real rebase of this very branch listed three of main's merge commits
    for replay. `--onto` says what the branch was actually rebased onto.
    """
    gh = FakeGH({"heads/main": "mainsha", "heads/feat/x": "stale-remote-head"},
                commits=["mainsha", "stale-remote-head"])
    code, gh = _run_main(monkeypatch, gh, branch="feat/x", revs=["c1sha"],
                         extra_argv=["--onto", "mainsha"])
    assert code == 0
    # The first replayed commit must parent on the named base, NOT on the stale head.
    parents = [c["parents"] for c in gh.created_commits]
    assert parents[0] == ["mainsha"], (
        f"with --onto mainsha the replay must start at mainsha, not at the stale "
        f"remote head: {parents!r}")
    assert all("stale-remote-head" not in p for p in parents), (
        f"nothing may parent on the pre-rebase head: {parents!r}")


def test_the_diff_base_follows_onto_so_the_replay_is_not_a_diff_against_the_old_head(
        monkeypatch):
    """--onto that only changed the parent would still diff against the stale head.

    The parent and the diff base must be the SAME commit, or the commit's tree
    describes a change relative to one base while claiming another as its parent --
    a commit that is internally inconsistent and silently wrong.
    """
    seen = []
    gh = FakeGH({"heads/main": "mainsha", "heads/feat/x": "stale-remote-head"},
                commits=["mainsha", "stale-remote-head"])

    def fake_git(*args, binary=False):
        if args[0] == "diff":
            seen.append(args)
            return raw(":100644 100644 aaa bbb M", "a.py") if binary else ""
        if args[0] == "config":
            return "https://github.com/acme/llmops.git"
        if args[:2] == ("rev-parse", "HEAD"):
            return "headsha"
        if args[:2] == ("rev-parse", "HEAD^"):
            return "parentsha"
        if args[0] == "rev-parse" and args[1].endswith("^{tree}"):
            return "local-tree"
        if args[:2] == ("rev-list", "--count"):
            return "7"
        if args[:2] == ("rev-list", "--parents"):
            return f"{args[3]} local-parent"
        if args[:2] == ("rev-list", "--reverse"):
            return "c1sha"
        if args[0] == "show":
            return b"content" if binary else "content"
        if args[0] == "log":
            return "a message"
        raise AssertionError(f"unexpected git {args}")

    monkeypatch.setattr(push, "git", fake_git)
    monkeypatch.setattr(push, "GitHub", lambda repo, token: gh)
    monkeypatch.setattr(push.subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0, "stdout": "tok\n"})())
    push.main(["--branch", "feat/x", "--onto", "mainsha"])
    bases = [a[4] for a in seen if len(a) > 4]
    assert bases and bases[0] == "mainsha", (
        f"the first diff must be against the --onto base: {seen!r}")
    assert all("stale-remote-head" not in a for a in bases), (
        f"no diff may use the pre-rebase head as its base: {seen!r}")


def test_rev_list_is_scoped_to_onto_so_the_merges_rebased_onto_are_not_replayed(
        monkeypatch):
    """The observed symptom: `rev-list stale..HEAD` includes main's merge commits.

    Those merges are already on the remote; replaying them as this branch's commits
    is how a rebased push turns 3 commits into 6 and attributes main's work here.
    """
    seen = []
    gh = FakeGH({"heads/main": "mainsha", "heads/feat/x": "stale-remote-head"},
                commits=["mainsha", "stale-remote-head"])

    def fake_git(*args, binary=False):
        if args[:2] == ("rev-list", "--reverse"):
            seen.append(args[-1])
            return "c1sha"
        if args[0] == "config":
            return "https://github.com/acme/llmops.git"
        if args[:2] == ("rev-parse", "HEAD"):
            return "headsha"
        if args[:2] == ("rev-parse", "HEAD^"):
            return "parentsha"
        if args[0] == "rev-parse" and args[1].endswith("^{tree}"):
            return "local-tree"
        if args[:2] == ("rev-list", "--count"):
            return "7"
        if args[:2] == ("rev-list", "--parents"):
            return f"{args[3]} local-parent"
        if args[0] == "diff":
            return raw(":100644 100644 aaa bbb M", "a.py") if binary else ""
        if args[0] == "show":
            return b"content" if binary else "content"
        if args[0] == "log":
            return "a message"
        raise AssertionError(f"unexpected git {args}")

    monkeypatch.setattr(push, "git", fake_git)
    monkeypatch.setattr(push, "GitHub", lambda repo, token: gh)
    monkeypatch.setattr(push.subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0, "stdout": "tok\n"})())
    push.main(["--branch", "feat/x", "--onto", "mainsha"])
    assert seen == ["mainsha..headsha"], (
        f"the commit list must be scoped to the --onto base: {seen!r}")


def test_onto_still_moves_the_branch_ref_to_the_new_tip(monkeypatch):
    """A rebase push is a ref REWRITE: the branch must end on the replayed tip.

    Leaving the ref where it was would make the whole replay invisible while the
    tool reported success.
    """
    gh = FakeGH({"heads/main": "mainsha", "heads/feat/x": "stale-remote-head"},
                commits=["mainsha", "stale-remote-head"])
    code, gh = _run_main(monkeypatch, gh, branch="feat/x", revs=["c1sha", "c2sha"],
                         extra_argv=["--onto", "mainsha"])
    assert code == 0
    tip = gh.created_commits[-1]
    assert gh.refs["heads/feat/x"] not in ("stale-remote-head", "mainsha"), (
        f"the branch still points at a pre-rebase commit: {gh.refs!r}")
    assert gh.refs["heads/feat/x"] == tip["sha"], (
        f"the branch must point at the last replayed commit: {gh.refs!r}")


def test_only_an_onto_push_forces_the_ref(monkeypatch):
    """A rebase is by definition not a fast forward, so the PATCH needs force=True.

    Live 422 on 2026-08-01: "Update is not a fast forward". But force must be scoped
    to --onto and nowhere else -- an ordinary advance that is somehow non-fast-forward
    means the remote has commits this run did not see, and forcing there deletes them.
    """
    gh = FakeGH({"heads/main": "mainsha", "heads/feat/x": "stale-remote-head"},
                commits=["mainsha", "stale-remote-head"])
    _run_main(monkeypatch, gh, branch="feat/x", revs=["c1sha"],
              extra_argv=["--onto", "mainsha"])
    patches = [c for c in gh.calls if c[0] == "PATCH"]
    assert patches and patches[-1][2].get("force") is True, (
        f"a rebase push must force the ref or the PATCH 422s: {patches!r}")

    plain = FakeGH({"heads/main": "mainsha", "heads/feat/x": "remotesha"},
                   commits=["parentsha", "remotesha"])
    _run_main(monkeypatch, plain, branch="feat/x")
    plain_patches = [c for c in plain.calls if c[0] == "PATCH"]
    assert plain_patches and not plain_patches[-1][2].get("force"), (
        f"an ordinary advance must NOT force: a non-fast-forward there means the "
        f"remote has commits we never saw, and forcing deletes them: {plain_patches!r}")


def test_onto_refuses_a_base_the_remote_does_not_have(monkeypatch):
    """A typo'd --onto must fail before any commit is built.

    Basing on a sha the remote cannot resolve produces commits whose parent does not
    exist there -- rejected late, after blobs have already been uploaded.

    The blob assertion is the one that bites. Without an upfront check the run still
    dies, because the base_tree lookup 404s on the same sha -- so "it raised
    SystemExit naming typosha" is true either way and proves nothing. What differs is
    WHEN: the late failure happens after tree_entries() has already POSTed every
    changed file as a blob, leaving orphaned objects on the repo for a typo.
    """
    gh = FakeGH({"heads/main": "mainsha", "heads/feat/x": "stale-remote-head"},
                commits=["mainsha", "stale-remote-head"])
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, gh, branch="feat/x", revs=["c1sha"],
                  extra_argv=["--onto", "typosha"])
    assert "typosha" in str(exc.value), (
        f"the error must name the base it could not find: {exc.value}")
    assert not gh.created_commits, "nothing may be created on a bad --onto"
    assert not [c for c in gh.calls if c[1] == "/git/blobs"], (
        f"a bad --onto must be caught BEFORE any blob is uploaded; these went up "
        f"anyway: {[c for c in gh.calls if c[1] == '/git/blobs']!r}")


# ── corruption #6: the fix for #4 made every push replay the whole branch ──────
# Replaying commit-by-commit gives each remote commit a NEW sha, so the remote head is
# never an ancestor of local HEAD and `rev-list remote..HEAD` returns everything again.
# Observed live 2026-08-01: pushing 1 new commit to docs/no-s3-skill-mirror landed 2, and
# the previous commit appeared on the remote twice under its own message. Tree parity is
# blind to it for the same reason it was blind to the squash -- the final tree is
# identical either way -- which is why the prefix is now matched by TREE.

def test_a_commit_the_remote_already_has_is_not_replayed_a_second_time(monkeypatch):
    """The live defect. Two local commits, the first already on the remote (as a replayed
    commit with a different sha but the SAME tree): only the second may be created."""
    gh = FakeGH({"heads/docs/x": "remote-c1"},
                commits=["basesha"],
                chain=[("remote-c1", "tree-c1", "basesha"),
                       ("basesha", "tree-base", None)])
    rc, gh = _run_main(monkeypatch, gh, branch="docs/x",
                       revs=["c1", "c2"],
                       trees={"c1": "tree-c1", "c2": "local-tree"},
                       messages={"c1": "first commit", "c2": "second commit"})
    assert rc == 0
    made = [c["message"] for c in gh.created_commits]
    assert made == ["second commit"], (
        f"the remote already had c1's tree, so only c2 should be created; got {made}. "
        "Replaying c1 puts it on the remote twice under its own message.")


def test_the_duplicate_is_caught_by_tree_because_the_shas_cannot_match(monkeypatch):
    """Why the check is by tree and not by sha, asserted rather than left to the comment.

    The remote commit's sha ("remote-c1") appears nowhere in the local rev list -- that is
    the whole nature of an API replay -- so any sha-based comparison finds no overlap and
    replays everything. The tool must never ask for a local object named by a remote sha.
    """
    gh = FakeGH({"heads/docs/x": "remote-c1"},
                commits=["basesha"],
                chain=[("remote-c1", "tree-c1", "basesha"),
                       ("basesha", "tree-base", None)])
    _run_main(monkeypatch, gh, branch="docs/x", revs=["c1", "c2"],
              trees={"c1": "tree-c1", "c2": "local-tree"})
    assert "remote-c1" not in [c["message"] for c in gh.created_commits]
    trees_read = [c[1] for c in gh.calls if c[1].startswith("/git/commits/")]
    assert "/git/commits/remote-c1" in trees_read, (
        "the remote chain must be READ to learn its trees; without that the tool has no "
        "way to know what it already pushed")


def test_only_a_prefix_is_dropped_so_a_revert_still_pushes(monkeypatch):
    """A revert restores an earlier tree. If the skip scanned the whole list instead of
    stopping at the first mismatch, the revert would be silently discarded -- the tool
    would decide "the remote already has this tree" about a commit whose entire purpose
    is to bring that tree back."""
    gh = FakeGH({"heads/docs/x": "remote-c1"},
                commits=["basesha"],
                chain=[("remote-c1", "tree-c1", "basesha"),
                       ("basesha", "tree-base", None)])
    rc, gh = _run_main(monkeypatch, gh, branch="docs/x",
                       revs=["c1", "c2", "c3"],
                       # c3 reverts c2, restoring the base tree the remote also has
                       trees={"c1": "tree-c1", "c2": "local-tree", "c3": "tree-base"},
                       messages={"c1": "first", "c2": "change", "c3": "Revert change"})
    assert rc == 0
    made = [c["message"] for c in gh.created_commits]
    assert made == ["change", "Revert change"], (
        f"expected c2 and c3 (the revert) to be pushed, got {made}. Scanning past the "
        "first mismatch drops the revert because its tree matches the remote's base.")


def test_an_ordinary_advance_still_pushes_every_new_commit(monkeypatch):
    """The guard must not become a filter that eats real work: when none of the local
    trees are on the remote, all of them are replayed, in order."""
    gh = FakeGH({"heads/docs/x": "remote-c1"},
                commits=["basesha"],
                chain=[("remote-c1", "tree-c1", "basesha"),
                       ("basesha", "tree-base", None)])
    rc, gh = _run_main(monkeypatch, gh, branch="docs/x",
                       revs=["c2", "c3"],
                       trees={"c2": "tree-c2", "c3": "local-tree"},
                       messages={"c2": "second", "c3": "third"})
    assert rc == 0
    assert [c["message"] for c in gh.created_commits] == ["second", "third"]


def test_the_skip_reports_what_it_dropped(monkeypatch, capsys):
    """A silent skip is indistinguishable from a push that lost a commit. Whichever way
    this goes wrong, the operator must be able to see it in the output."""
    gh = FakeGH({"heads/docs/x": "remote-c1"},
                commits=["basesha"],
                chain=[("remote-c1", "tree-c1", "basesha"),
                       ("basesha", "tree-base", None)])
    _run_main(monkeypatch, gh, branch="docs/x", revs=["c1", "c2"],
              trees={"c1": "tree-c1", "c2": "local-tree"},
              messages={"c1": "already pushed", "c2": "new work"})
    out = capsys.readouterr().out
    assert "skipping" in out and "already pushed" in out, (
        f"the skip must be visible; got:\n{out}")


def test_a_remote_chain_walk_that_404s_does_not_abort_the_push(monkeypatch):
    """The walk is an optimization on top of a correct push, not a precondition for one.

    A shallow or unreadable remote chain (a 404 partway back) must degrade to "replay
    what rev-list said" rather than raise -- refusing to push because history could not
    be walked would be a worse failure than the duplicate it prevents.
    """
    gh = FakeGH({"heads/docs/x": "remote-c1"},
                commits=[],
                chain=[("remote-c1", "tree-c1", "missing-parent")])
    rc, gh = _run_main(monkeypatch, gh, branch="docs/x", revs=["c2"],
                       trees={"c2": "local-tree"}, messages={"c2": "new work"})
    assert rc == 0
    assert [c["message"] for c in gh.created_commits] == ["new work"]


# --- defect 7: a merge flattened, twice over ------------------------------------
#
# Two failures with one cause -- nothing knew a commit can have more than one parent.
# The replay RANGE walked all ancestry (so main's commits replayed as this branch's),
# and each replayed commit was built with exactly one parent (so the merge itself
# landed linear). Tree parity is blind to both: the final tree is identical either way.
# Observed 2026-08-02 on fix/eval-generate-dispatch -- `compare main...branch` read
# "diverged, ahead 2, behind 2" for a branch that contained every commit in main.

def test_a_merge_keeps_its_second_parent(monkeypatch):
    gh = FakeGH({"heads/feat/x": "remote-head"}, commits=["remote-head", "mainsha"])
    code, gh = _run_main(monkeypatch, gh, branch="feat/x", revs=["mergesha"],
                         parents={"mergesha": ["local-first", "mainsha"]})
    assert code == 0
    assert gh.created_commits[0]["parents"] == ["remote-head", "mainsha"], (
        "the merge parent is gone: the commit landed linear, so main stops being an "
        f"ancestor of this branch. parents={gh.created_commits[0]['parents']}")


def test_an_ordinary_push_stays_single_parent(monkeypatch):
    """The first LOCAL parent must not be sent alongside remote_sha.

    The remote's commits are API-built, so local HEAD^ and the remote head are different
    shas for the same position (defect 2). Carrying the local first parent would give
    every ordinary linear push a bogus second parent -- an over-correction that turns
    every push into a fake merge. This caught exactly that bug in the first draft.
    """
    gh = FakeGH({"heads/feat/x": "remote-head"}, commits=["remote-head", "parentsha"])
    code, gh = _run_main(monkeypatch, gh, branch="feat/x")
    assert code == 0
    assert gh.created_commits[0]["parents"] == ["remote-head"]


def test_an_octopus_merge_keeps_every_parent(monkeypatch):
    gh = FakeGH({"heads/feat/x": "remote-head"},
                commits=["remote-head", "p2", "p3"])
    code, gh = _run_main(monkeypatch, gh, branch="feat/x", revs=["mergesha"],
                         parents={"mergesha": ["local-first", "p2", "p3"]})
    assert code == 0
    assert gh.created_commits[0]["parents"] == ["remote-head", "p2", "p3"]


def test_a_merge_parent_the_remote_cannot_resolve_is_dropped_not_sent(monkeypatch):
    """The API rejects an unknown parent sha, which would fail the whole push.

    A local-only side branch is a worse reason to refuse than to land the commit as the
    linear advance it effectively is -- so the unresolvable parent is dropped.
    """
    gh = FakeGH({"heads/feat/x": "remote-head"}, commits=["remote-head"])
    code, gh = _run_main(monkeypatch, gh, branch="feat/x", revs=["mergesha"],
                         parents={"mergesha": ["local-first", "local-only-branch"]})
    assert code == 0
    assert gh.created_commits[0]["parents"] == ["remote-head"]


def test_the_dropped_parent_is_reported(monkeypatch, capsys):
    """Dropping it silently reproduces the defect: a merge that reads as linear, with
    nothing in the output to say so."""
    gh = FakeGH({"heads/feat/x": "remote-head"}, commits=["remote-head"])
    _run_main(monkeypatch, gh, branch="feat/x", revs=["mergesha"],
              parents={"mergesha": ["local-first", "local-only-branch"]})
    err = capsys.readouterr().err
    assert "local-only" in err and "linear" in err, (
        f"the dropped parent must be visible; got:\n{err}")


def test_a_merge_that_changes_no_file_is_still_pushed(monkeypatch):
    """Merging a branch already contained changes no file, and is the common case.

    "Nothing to push" is a statement about the TREE; what this commit carries is the
    ANCESTRY. Returning early would leave the remote at a commit the merged branch is not
    an ancestor of -- defect 7's wrong ancestry, reached by an early return.
    """
    # An empty diff means the base tree already IS local HEAD's tree, so the double has
    # to say that or the parity check fails on the fixture rather than on the tool.
    gh = FakeGH({"heads/feat/x": "remote-head"}, commits=["remote-head", "mainsha"],
                chain=[("remote-head", "local-tree", None)])
    code, gh = _run_main(monkeypatch, gh, branch="feat/x", revs=["mergesha"],
                         ops=(), trees={"mergesha": "local-tree"},
                         parents={"mergesha": ["local-first", "mainsha"]})
    assert code == 0
    assert len(gh.created_commits) == 1, "the merge commit was never created"
    assert gh.created_commits[0]["parents"] == ["remote-head", "mainsha"]


def test_an_empty_diff_with_no_merge_still_pushes_nothing(monkeypatch):
    """The no-op check must stay a no-op check. Widening it for merges must not make
    every already-matching branch push an empty commit on every run."""
    gh = FakeGH({"heads/feat/x": "remote-head"}, commits=["remote-head"])
    code, gh = _run_main(monkeypatch, gh, branch="feat/x", ops=())
    assert code == 0
    assert gh.created_commits == []


def test_a_squash_does_not_claim_the_merge_parents(monkeypatch):
    """--message squashes N commits into one, so that commit does not represent the
    merges inside the range; carrying their parents would assert an ancestry the
    squashed tree does not have."""
    gh = FakeGH({"heads/feat/x": "remote-head"}, commits=["remote-head", "mainsha"])
    code, gh = _run_main(monkeypatch, gh, branch="feat/x",
                         parents={"headsha": ["local-first", "mainsha"]},
                         extra_argv=["--message", "one squashed commit"])
    assert code == 0
    assert gh.created_commits[0]["parents"] == ["remote-head"]


def test_the_local_first_parent_is_not_sent_even_when_the_remote_has_it(monkeypatch):
    """The `[2:]` slice, pinned where it is actually observable.

    `test_an_ordinary_push_stays_single_parent` does not catch a `[1:]` slice: the local
    first parent is not on the remote in that fixture, so the bogus extra parent is
    dropped by the resolvability filter and the bug hides behind the safety net. On the
    CREATE path the two coincide -- the base IS local HEAD^ -- so a `[1:]` slice sends a
    resolvable sha and the commit lands with its own base listed twice. That duplicate is
    what makes the parent list wrong rather than merely redundant, and this is the only
    fixture where it can be seen.
    """
    gh = FakeGH({"heads/main": "mainsha"}, commits=["parentsha"])
    code, gh = _run_main(monkeypatch, gh, branch="feat/new",
                         parents={"headsha": ["parentsha"]})
    assert code == 0
    assert gh.created_commits[0]["parents"] == ["parentsha"], (
        "the local first parent was carried as a merge parent: the commit lists its own "
        f"base twice. parents={gh.created_commits[0]['parents']}")
