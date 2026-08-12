#!/usr/bin/env python3
"""Every PR labelled MERGED must have reached the default branch.

    python3 tools/audit_landed.py [--days 30] [--repo owner/name] [--json out.json]

`MERGED` and `landed on main` are different facts, and only one of them is on the
PR list. On 2026-08-12 this repo merged a twelve-deep stack bottom-up in ~50
seconds; all twelve read MERGED and only #92 reached `main`, leaving 28 files
that `main` did not serve. The cause is mechanical: GitHub retargets a child
PR's base to the parent's base only when the parent's HEAD BRANCH IS DELETED, so
with `delete_branch_on_merge` off every later merge commit landed on a branch
that had itself been merged a second earlier. The same shape had already
happened once, on 2026-08-11 (#76/#77/#78/#83), and nobody noticed.

So the question is not "was it merged" but "is it reachable from the default
branch". Ancestry is asked of git when a full clone is available and of the
compare API otherwise; the git path is preferred because it is exact, offline,
and costs no request per PR. It needs real history: `actions/checkout` clones at
depth 1, where `git cat-file` cannot see the commit and every ancestry question
falls through to the API, so CI passes `fetch-depth: 0`.

Exit 1 if any merged PR is unreachable OR unresolvable. A check that cannot
answer must not report clean.
"""
import argparse
import datetime
import json
import subprocess
import sys

#: `gh pr list` is paginated by --limit and truncates in silence. Ask for far
#: more than any window holds and SAY SO when the ceiling is hit: a census that
#: quietly stops at N reports "all landed" for the N most recent merges and
#: nothing about the rest, which reads exactly like coverage.
PR_LIMIT = 200

#: The compare API's `commits` array stops at 250 entries. The verdict here is
#: read from `behind_by`, an aggregate over the whole comparison, so the cap does
#: not change the answer -- but only these statuses mean the aggregate was
#: computed at all. Anything else (an error body, a schema change) is unresolved,
#: never False.
COMPARE_STATUSES = ("identical", "ahead", "behind", "diverged")


def gh(args, check=True):
    out = subprocess.run(["gh"] + args, capture_output=True, text=True)
    if check and out.returncode != 0:
        raise SystemExit("gh %s failed: %s" % (" ".join(args), out.stderr.strip()))
    return out


def repo_slug(explicit):
    if explicit:
        return explicit
    return gh(["repo", "view", "--json", "nameWithOwner",
               "-q", ".nameWithOwner"]).stdout.strip()


def default_branch(slug):
    return gh(["api", "repos/%s" % slug, "--jq", ".default_branch"]).stdout.strip()


def cutoff_iso(days, now=None):
    """The window's lower bound as a GitHub timestamp, or None for "no window".

    Computed here rather than shelled out to `date`: the BSD form (`-v-30d`) and
    the GNU form (`-d '30 days ago'`) are not the same flag, and a tool that
    guesses which one the runner has will one day guess wrong on the platform
    nobody tested.
    """
    if not days:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return (now - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def within_window(prs, since):
    """PRs merged at or after `since`.

    A PR with no mergedAt is dropped rather than compared: `None >= str` raises
    on Python 3, and the whole audit would die on one odd row.
    """
    if since is None:
        return list(prs)
    return [p for p in prs if (p.get("mergedAt") or "") >= since]


def merged_prs(slug, days, warn=None):
    out = gh(["pr", "list", "--repo", slug, "--state", "merged", "--limit", str(PR_LIMIT),
              "--json",
              "number,title,mergedAt,mergeCommit,baseRefName,headRefName,headRefOid"])
    prs = json.loads(out.stdout)
    if len(prs) >= PR_LIMIT and warn:
        warn("gh returned the full --limit of %d merged PRs, so this window may be "
             "TRUNCATED: anything merged before the oldest row below was not audited. "
             "Raise PR_LIMIT or lower --days." % PR_LIMIT)
    return within_window(prs, cutoff_iso(days))


def have_full_clone():
    """A depth-1 clone cannot answer an ancestry question, so say which we have."""
    out = subprocess.run(["git", "rev-parse", "--is-shallow-repository"],
                         capture_output=True, text=True)
    return out.returncode == 0 and out.stdout.strip() == "false"


def is_ancestor_git(sha, branch):
    """None when git cannot see the object -- never a silent False.

    Measured on git 2.x: `merge-base --is-ancestor` exits **128** ("fatal: Not a
    valid commit name") for an object the repo does not have, and 1 only for a
    real "no". So the `rc in (0, 1)` test below is already enough on this git, and
    the existence check in front of it is deliberately redundant: it is here for
    the version that answers 1 instead, where a shallow clone would otherwise be
    told confidently that every unfetched commit is NOT an ancestor -- a
    repo-wide false alarm that never reaches the API fallback. Unresolved is the
    right answer to a question git cannot answer.

    Because it is redundant on today's git, deleting it does not change any
    real-git outcome; `test_a_git_that_reports_1_for_a_missing_object_is_not_believed`
    injects the lying git that makes the branch reachable.
    """
    if subprocess.run(["git", "cat-file", "-e", "%s^{commit}" % sha],
                      capture_output=True).returncode != 0:
        return None
    out = subprocess.run(["git", "merge-base", "--is-ancestor", sha, branch],
                         capture_output=True)
    if out.returncode in (0, 1):
        return out.returncode == 0
    return None


def ancestor_from_compare(payload):
    """`sha` is an ancestor of `branch` iff the comparison sha...branch is not behind.

    In `compare/A...B`, `behind_by` counts what A has that B does not, so A is an
    ancestor of B exactly when it is 0 (status `identical` or `ahead`).
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("status") not in COMPARE_STATUSES:
        return None
    behind = payload.get("behind_by")
    if not isinstance(behind, int) or isinstance(behind, bool):
        return None
    return behind == 0


def is_ancestor_api(slug, sha, branch):
    out = gh(["api", "repos/%s/compare/%s...%s" % (slug, sha, branch)], check=False)
    if out.returncode != 0:
        return None
    try:
        return ancestor_from_compare(json.loads(out.stdout))
    except ValueError:
        return None


def ancestor(slug, sha, branch, use_git):
    """True/False/None, git first, compare API as the fallback."""
    if not sha:
        return None
    if use_git:
        got = is_ancestor_git(sha, branch)
        if got is not None:
            return got
    return is_ancestor_api(slug, sha, branch)


def classify(pr, m_anc, h_anc, branch):
    """Two questions, not one.

    "Is the MERGE COMMIT an ancestor of the default branch" answers whether that
    merge landed. It is NOT the same as whether the work landed: when a stack is
    merged bottom-up and then rescued by merging the tip, each child's merge
    commit stays on a side branch forever while its HEAD commits become
    ancestors through the rescue. #76/#77/#78/#83 on 2026-08-11 and #93..#102 on
    2026-08-12 were exactly that, and calling them lost would be wrong.

    So the verdict is merge-commit OR head reachable, and the merge-commit-only
    miss is reported as residue -- present tense evidence that a stack collapsed,
    even though nothing is missing. Caveat: a squash or rebase merge rewrites the
    head into new SHAs, so head-reachability is expected to be false there and
    the merge commit is the only usable signal; this repo merges with merge
    commits.
    """
    if m_anc is True:
        landed, how = True, "merge commit on %s" % branch
    elif h_anc is True:
        landed, how = True, ("head reachable; merge commit stranded on '%s'"
                             % pr["baseRefName"])
    elif m_anc is None and h_anc is None:
        landed, how = None, "could not be resolved"
    else:
        landed, how = False, "neither the merge commit nor the head is on %s" % branch
    msha = (pr.get("mergeCommit") or {}).get("oid")
    hsha = pr.get("headRefOid")
    return dict(number=pr["number"], landed=landed,
                sha=(msha or "")[:8] or None, head_sha=(hsha or "")[:8] or None,
                base=pr["baseRefName"], head=pr["headRefName"],
                merged_at=pr.get("mergedAt"), how=how, title=pr["title"],
                residue=(landed is True and m_anc is not True))


def audit(slug, days, use_git, warn=None):
    branch = default_branch(slug)
    rows = []
    for pr in merged_prs(slug, days, warn=warn):
        msha = (pr.get("mergeCommit") or {}).get("oid")
        m_anc = ancestor(slug, msha, branch, use_git)
        # The head is only asked about when the merge commit did not answer yes:
        # one request per PR is the cost of this audit, and half of them are
        # already decided.
        h_anc = ancestor(slug, pr.get("headRefOid"), branch, use_git) \
            if m_anc is not True else None
        rows.append(classify(pr, m_anc, h_anc, branch))
    return branch, rows


def report(slug, branch, rows, out=print):
    """Print the audit and return the exit code."""
    lost = [r for r in rows if r["landed"] is False]
    unknown = [r for r in rows if r["landed"] is None]
    residue = [r for r in rows if r["residue"]]
    out("%s: %d merged PR(s) audited against %s" % (slug, len(rows), branch))
    for r in rows:
        mark = {True: "residue" if r["residue"] else "landed ",
                False: "NOT ON ", None: "UNKNOWN"}[r["landed"]]
        out("  %s #%-4s %-9s base=%-42s %s"
            % (mark, r["number"], r["sha"] or "-", r["base"], r["merged_at"] or ""))

    if lost:
        out("\n%d merged PR(s) are NOT reachable from %s:" % (len(lost), branch))
        for r in lost:
            out("  #%s merged into '%s' -- %s" % (r["number"], r["base"], r["title"]))
        out("\nA stack lands only if each PR's base is retargeted as the one below it "
            "merges, and GitHub does that only when the parent's head branch is "
            "deleted. Check: gh api repos/%s --jq .delete_branch_on_merge" % slug)
    if unknown:
        out("\n%d merged PR(s) could not be resolved -- neither git nor the compare "
            "API answered. This is not a pass: in CI check out with fetch-depth: 0 "
            "and make sure the token can read this repo." % len(unknown))
        for r in unknown:
            out("  #%s merge=%s head=%s" % (r["number"], r["sha"], r["head_sha"]))
    if residue:
        out("\n%d merged PR(s) landed only because a later merge carried them up; their "
            "own merge commit is stranded on a side branch -- the signature of a "
            "bottom-up stack that was rescued:" % len(residue))
        for r in residue:
            out("  #%s  merge %s stranded on '%s', head %s reachable"
                % (r["number"], r["sha"], r["base"], r["head_sha"]))
    if lost or unknown:
        return 1
    out("\nall audited PRs are reachable from %s" % branch)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30,
                    help="window of merged PRs to audit; 0 = everything gh returns")
    ap.add_argument("--repo", default=None)
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args()

    slug = repo_slug(args.repo)
    use_git = have_full_clone()
    warnings = []

    def warn(msg):
        warnings.append(msg)
        print("WARNING: %s" % msg)

    if not use_git:
        warn("shallow or absent clone -- ancestry falls back to the compare API, "
             "one request per PR. In CI use fetch-depth: 0.")
    branch, rows = audit(slug, args.days if args.days else None, use_git, warn=warn)
    rc = report(slug, branch, rows)
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(dict(repo=slug, default_branch=branch, rows=rows,
                           warnings=warnings), fh, indent=2)
    return rc


if __name__ == "__main__":
    sys.exit(main())
