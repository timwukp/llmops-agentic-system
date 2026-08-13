"""Guards for the landed-PR detector.

The detector exists because "12 PRs MERGED" and "12 PRs on main" were different
facts and only the first one was visible. Its own first run had the mirror-image
defect: it read the merge commit alone, so the four PRs (#76/#77/#78/#83) that a
later merge had carried up were reported as LOST. A detector with a
false-positive class is worse than no detector, because the next real collapse
arrives inside a list of cries that were wrong before.

So both directions are asserted here: a rescued stack must read landed-with-
residue, and a genuinely stranded PR must read not-landed. Nothing in this file
reaches the network -- every ancestry answer is injected, except the one test
that runs real `git` in a throwaway repo, because the whole point of
`is_ancestor_git` is what the real tool does with a commit it cannot see.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

import audit_landed as al  # noqa: E402


def pr(number, base="main", head="feat/x", merge="a" * 40, head_oid="b" * 40,
       merged_at="2026-08-12T15:00:00Z", title="t"):
    return {"number": number, "title": title, "mergedAt": merged_at,
            "mergeCommit": {"oid": merge} if merge else None,
            "baseRefName": base, "headRefName": head, "headRefOid": head_oid}


# ── the verdict ─────────────────────────────────────────────────────────────
def test_a_rescued_bottom_up_stack_reads_landed_with_residue():
    """#93..#102's exact shape: merge commit stranded, head carried up by the rescue.

    This is the case the first version of the tool got wrong. If the verdict ever
    goes back to reading the merge commit alone, this test is what says so --
    dropping the head branch of `classify` turns this row from landed into LOST.
    """
    row = al.classify(pr(101, base="fix/alarm-the-control-plane"),
                      m_anc=False, h_anc=True, branch="main")
    assert row["landed"] is True
    assert row["residue"] is True
    assert "stranded on 'fix/alarm-the-control-plane'" in row["how"]


def test_residue_is_false_when_the_merge_commit_itself_is_on_main():
    """Otherwise every ordinary PR is flagged, and the signal means nothing.

    `residue` is `landed and merge-commit NOT reachable`. Drop the second half
    and all 34 audited PRs report residue; drop the first and a lost PR reports
    it too.
    """
    row = al.classify(pr(105), m_anc=True, h_anc=None, branch="main")
    assert row["landed"] is True
    assert row["residue"] is False
    assert row["how"] == "merge commit on main"


def test_neither_the_merge_commit_nor_the_head_on_main_is_not_landed():
    row = al.classify(pr(99, base="fix/parent"), m_anc=False, h_anc=False, branch="main")
    assert row["landed"] is False
    assert row["residue"] is False


def test_an_unresolvable_ancestry_is_reported_as_unknown_not_landed():
    """A missing answer is not a pass. It is also not a failure to land."""
    row = al.classify(pr(90), m_anc=None, h_anc=None, branch="main")
    assert row["landed"] is None
    assert row["how"] == "could not be resolved"
    assert row["residue"] is False


def test_a_half_resolved_pr_is_not_landed_rather_than_unknown():
    """merge commit unresolvable, head answered False -- the head answer decides.

    `m_anc is None and h_anc is None` is the only unknown. Widening it to `or`
    would turn this row into UNKNOWN and hide a real miss behind an
    infrastructure complaint.
    """
    row = al.classify(pr(91), m_anc=None, h_anc=False, branch="main")
    assert row["landed"] is False


# ── the compare-API verdict ─────────────────────────────────────────────────
def test_the_compare_verdict_reads_behind_by_not_ahead_by():
    """In compare/A...B, A is an ancestor of B iff `behind_by` is 0.

    `ahead_by` is the other direction and is nonzero for every normal merge, so a
    tool reading it answers False for the entire repo -- or, with the comparison
    written the other way round, True for everything.
    """
    assert al.ancestor_from_compare(
        {"status": "ahead", "behind_by": 0, "ahead_by": 16}) is True
    assert al.ancestor_from_compare(
        {"status": "identical", "behind_by": 0, "ahead_by": 0}) is True
    assert al.ancestor_from_compare(
        {"status": "diverged", "behind_by": 3, "ahead_by": 0}) is False
    assert al.ancestor_from_compare(
        {"status": "behind", "behind_by": 7, "ahead_by": 0}) is False


@pytest.mark.parametrize("payload", [
    {"behind_by": 0},                                  # no status at all
    {"status": "not-a-status", "behind_by": 0},         # a schema we do not know
    {"status": "ahead"},                                # aggregate absent
    {"status": "ahead", "behind_by": "0"},              # a string is not a count
    {"status": "ahead", "behind_by": True},             # bool is an int in Python
    {"message": "Not Found"},                           # an error body
    None,
    [],
])
def test_an_unrecognised_compare_payload_is_unresolved_never_true(payload):
    """The dangerous direction is a wrong TRUE: it reports a lost PR as landed.

    `behind_by == 0` is True for `True`, for `0`, and for a missing key compared
    with a permissive default -- so the type is checked, not just the value.
    """
    assert al.ancestor_from_compare(payload) is None


# ── the window ──────────────────────────────────────────────────────────────
def test_the_window_is_computed_in_python_not_by_the_date_binary():
    """`date -v-30d` is BSD and `date -d '30 days ago'` is GNU; neither is both."""
    import datetime
    now = datetime.datetime(2026, 8, 12, 16, 30, 0, tzinfo=datetime.timezone.utc)
    assert al.cutoff_iso(1, now=now) == "2026-08-11T16:30:00Z"
    assert al.cutoff_iso(30, now=now) == "2026-07-13T16:30:00Z"
    assert al.cutoff_iso(0, now=now) is None


def test_a_pr_merged_one_second_before_the_cutoff_is_excluded():
    since = "2026-08-12T00:00:00Z"
    rows = al.within_window(
        [pr(1, merged_at="2026-08-11T23:59:59Z"),
         pr(2, merged_at="2026-08-12T00:00:00Z"),
         pr(3, merged_at="2026-08-12T00:00:01Z")], since)
    assert [r["number"] for r in rows] == [2, 3]


def test_a_pr_with_no_merged_at_is_dropped_rather_than_crashing_the_audit():
    """`None >= "2026-.."` raises TypeError, which would kill the whole run."""
    rows = al.within_window([pr(1, merged_at=None), pr(2)], "2026-08-01T00:00:00Z")
    assert [r["number"] for r in rows] == [2]


def test_no_window_keeps_every_row_including_undated_ones():
    rows = al.within_window([pr(1, merged_at=None), pr(2)], None)
    assert len(rows) == 2


# ── truncation ──────────────────────────────────────────────────────────────
def test_a_census_that_hits_the_page_limit_says_so(monkeypatch):
    """Silent truncation reads exactly like coverage.

    `gh pr list --limit N` stops at N with no marker in the payload, so a repo
    with more merges than the limit gets a clean report about its newest N and
    silence about the rest.
    """
    class Out:
        stdout = "[%s]" % ",".join(
            '{"number":%d,"title":"t","mergedAt":"2026-08-12T15:00:00Z",'
            '"mergeCommit":{"oid":"aaa"},"baseRefName":"main",'
            '"headRefName":"h","headRefOid":"bbb"}' % i for i in range(al.PR_LIMIT))

    monkeypatch.setattr(al, "gh", lambda *a, **k: Out())
    said = []
    rows = al.merged_prs("o/r", 0, warn=said.append)
    assert len(rows) == al.PR_LIMIT
    assert said and "TRUNCATED" in said[0]


def test_a_census_under_the_limit_does_not_cry_truncation(monkeypatch):
    class Out:
        stdout = ('[{"number":1,"title":"t","mergedAt":"2026-08-12T15:00:00Z",'
                  '"mergeCommit":{"oid":"aaa"},"baseRefName":"main",'
                  '"headRefName":"h","headRefOid":"bbb"}]')

    monkeypatch.setattr(al, "gh", lambda *a, **k: Out())
    said = []
    al.merged_prs("o/r", 0, warn=said.append)
    assert said == []


# ── the exit code ───────────────────────────────────────────────────────────
def _report(rows):
    lines = []
    rc = al.report("o/r", "main", rows, out=lines.append)
    return rc, "\n".join(lines)


def test_residue_alone_exits_zero_because_nothing_is_missing():
    """Residue is history. Failing on it would red every CI run forever."""
    rc, text = _report([al.classify(pr(101, base="fix/p"), False, True, "main")])
    assert rc == 0
    assert "stranded" in text
    assert "all audited PRs are reachable from main" in text


def test_a_lost_pr_exits_one_and_names_the_repo_setting_to_check():
    rc, text = _report([al.classify(pr(99, base="fix/p"), False, False, "main")])
    assert rc == 1
    assert "NOT reachable" in text
    assert "delete_branch_on_merge" in text


def test_an_unresolved_pr_also_exits_one():
    """A guard that cannot run must not report clean.

    This is the mutant worth worrying about: `if lost: return 1` alone passes a
    CI run in which every single ancestry question went unanswered -- a depth-1
    checkout with no `gh` auth reports a perfectly green audit of nothing.
    """
    rc, text = _report([al.classify(pr(90), None, None, "main")])
    assert rc == 1
    assert "not a pass" in text
    assert "fetch-depth: 0" in text
    assert "all audited PRs are reachable" not in text


def test_an_empty_audit_is_not_a_silent_success():
    """No merged PRs in the window is a legitimate zero, and it must LOOK like one."""
    rc, text = _report([])
    assert rc == 0
    assert "0 merged PR(s) audited" in text


# ── real git, not a double ──────────────────────────────────────────────────
def test_git_ancestry_answers_none_for_a_commit_it_cannot_see(tmp_path, monkeypatch):
    """`merge-base --is-ancestor` exits 1 for "no" AND for "no such object".

    A shallow clone has almost no objects, so without the existence check first
    this function answers False for nearly every PR and the audit reports the
    whole repo as lost. Run against real git rather than a fake: a double would
    only echo back the exit codes this test already assumes.
    """
    def git(*args):
        return subprocess.run(
            ["git", "-c", "user.email=t@e", "-c", "user.name=t",
             "-c", "init.defaultBranch=main"] + list(args),
            cwd=tmp_path, capture_output=True, text=True, check=True)

    git("init", "-q", ".")
    (tmp_path / "a.txt").write_text("one\n")
    git("add", "a.txt")
    git("commit", "-q", "-m", "one")
    first = git("rev-parse", "HEAD").stdout.strip()
    (tmp_path / "a.txt").write_text("two\n")
    git("commit", "-qam", "two")
    second = git("rev-parse", "HEAD").stdout.strip()

    monkeypatch.chdir(tmp_path)
    assert al.is_ancestor_git(first, "main") is True
    assert al.is_ancestor_git(second, "main") is True      # a commit is its own ancestor

    # A well-formed sha that is not in this repo: unknown, NOT "not an ancestor".
    # Measured here: this git exits 128 for it, so the answer would be None even
    # without the existence check in front. That is why the check needs the
    # injected git below to be tested at all.
    assert al.is_ancestor_git("0" * 40, "main") is None
    probe = subprocess.run(["git", "merge-base", "--is-ancestor", "0" * 40, "main"],
                           cwd=tmp_path, capture_output=True)
    assert probe.returncode == 128, (
        "this git answers %d for a missing object, not 128. If it answers 1, the "
        "existence check in is_ancestor_git stops being redundant and starts being "
        "the only thing preventing a repo-wide false alarm" % probe.returncode)

    # And a commit that is genuinely off the branch answers False, so the
    # existence check has not simply swallowed every negative.
    git("checkout", "-q", "-b", "side", first)
    (tmp_path / "b.txt").write_text("side\n")
    git("add", "b.txt")
    git("commit", "-q", "-m", "side")
    off = git("rev-parse", "HEAD").stdout.strip()
    assert al.is_ancestor_git(off, "main") is False


def test_a_git_that_reports_1_for_a_missing_object_is_not_believed(monkeypatch):
    """The lying double, because real git cannot reach this branch.

    Real git exits 128 for an object it does not have (asserted above), so the
    existence check in `is_ancestor_git` is unreachable with a real repo and
    deleting it changes nothing observable -- a silently surviving mutant. The
    behaviour it protects is only visible against a git that answers 1, which is
    what a shallow clone would look like if `--is-ancestor` treated "absent" as
    "no". Injected here: without the check, this returns False and the audit
    declares a present commit lost.
    """
    calls = []

    class Fake:
        def __init__(self, rc):
            self.returncode = rc
            self.stdout = ""
            self.stderr = ""

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:2] == ["git", "cat-file"]:
            return Fake(1)          # git does not have the object
        if args[:2] == ["git", "merge-base"]:
            return Fake(1)          # ...and says "not an ancestor" anyway
        raise AssertionError("unexpected call %r" % (args,))

    monkeypatch.setattr(al.subprocess, "run", fake_run)
    assert al.is_ancestor_git("c" * 40, "main") is None, (
        "a git that cannot see the object answered 1, and the audit believed it")
    # The order matters: existence FIRST, so merge-base is never consulted.
    assert [c[1] for c in calls] == ["cat-file"]


# ── the CI wiring ───────────────────────────────────────────────────────────
def test_the_workflow_checks_out_full_history_or_the_audit_cannot_answer():
    """`actions/checkout` is depth 1 by default, where git sees no commit.

    The audit still runs there -- it falls through to the compare API -- but it
    pays one request per PR and loses the only exact answer it has. A workflow
    that drops `fetch-depth: 0` degrades in a way no test output would show, so
    the checkout depth is asserted, not documented.
    """
    wf = (REPO / ".github/workflows/landed-check.yml").read_text()
    assert "fetch-depth: 0" in wf
    assert "tools/audit_landed.py" in wf
    # Read access to pull requests is what `gh pr list` needs; without it the
    # audit resolves nothing and (correctly) reds for the wrong reason.
    assert "pull-requests: read" in wf
    # The failure this whole tool exists to catch happens ON MERGE, so a
    # pull_request-only trigger would never see it.
    assert "push:" in wf
