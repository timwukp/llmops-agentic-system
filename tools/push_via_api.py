#!/usr/bin/env python3
"""Push the current HEAD to a branch through the GitHub Git Data API.

`git push` is blocked in this environment, so every commit on a PR branch has gone
up as blobs -> tree -> commit -> PATCH ref. Doing that by hand is where two silent
corruptions live:

  1. A hardcoded file mode. The first four pushes on the Tasks branch sent every
     entry as 100644, which quietly dropped the executable bit from
     deploy/console/deploy.sh -- a deploy script that no longer runs, in a diff
     that renders as "0 insertions, 0 deletions".
  2. A base assumed to be HEAD~1. The remote branch head is whatever the last API
     commit produced, which is NOT the local HEAD~1 (different sha, and possibly a
     different tree if an earlier push mangled something). Diffing against the
     wrong base makes the push silently incomplete.
  3. A ref read believed on its first answer. GET /git/ref is eventually consistent:
     seconds after a branch is created it still 404s. Read once and a second push
     concludes the branch does not exist, bases its commit on the DEFAULT BRANCH, and
     tries to create the ref -- which would drop every commit already on the branch.
     Observed 2026-08-01 replaying three commits: push 2 took the create path and was
     saved only by POST /git/refs answering 422 "Reference already exists". A PATCH
     there instead would have silently orphaned push 1. So the read is retried, and a
     422 on create is treated as what it is -- proof the ref exists -- not as fatal.

  4. One commit for N local commits. Building a single commit from one base..HEAD diff
     and giving it HEAD's message silently discards every earlier message. Observed
     2026-08-01: two commits went up as `3b62181` carrying only the second's message,
     and the tree-parity check passed -- the final tree is identical whether you replay
     the commits or squash them, so that check is blind to this by construction. On a
     repo whose commit bodies carry the measurements justifying each change, that is
     evidence being dropped, not formatting.

  5. A rebased branch replayed onto its own stale remote head. After a local rebase the
     remote head is no longer an ancestor of HEAD, so base..HEAD includes every commit
     the branch was rebased ONTO. Observed 2026-08-01: the plan for a 3-commit branch
     listed three of main's merge commits for replay, which would have landed 6 commits
     and attributed main's merges to this branch. `--onto <sha>` states the real base;
     it is not inferred, because "not an ancestor" also describes a divergence whose
     commits must not be discarded.

  6. Every already-pushed commit replayed again on the next push -- caused by the fix for
     #4. A replayed commit gets a new sha, so the remote head is never an ancestor of
     local HEAD and base..HEAD returns the whole branch every time. Observed 2026-08-01:
     pushing 1 new commit landed 2, duplicating the previous commit under its own
     message. Tree parity is blind to it (the final tree is the same either way), so the
     prefix the remote already has is now matched by TREE, not by sha.

  7. A MERGE flattened, twice over. Two defects with one cause -- nothing here knew a
     commit can have more than one parent:

       * `rev-list base..HEAD` walks ALL ancestry, so merging main in put main's own
         commits into the replay list as though this branch had authored them; and
       * every replayed commit is built with `parents: [parent_sha]`, one parent, so the
         merge commit itself lands as an ordinary commit.

     Either alone loses the merge. Observed 2026-08-02 merging main into
     fix/eval-generate-dispatch: `compare main...branch` read "diverged, ahead 2, behind
     2" for a branch that contained every commit in main, and 174 lines from an
     already-merged PR appeared in the new PR's diff. So the replay range is
     `--first-parent` (this branch's own commits, not the merged branch's), and the merge
     parents beyond the first are carried onto the commit.

So this reads modes from git instead of assuming them, diffs against the branch's
actual remote head instead of assuming the local parent matches it, never concludes
"no such branch" from a single 404, replays each local commit as its own remote commit
with its own message, and keeps a merge a merge. A rename becomes delete-old + add-new;
a deletion becomes a null-sha tree entry.

What the tree-parity check at the end can and cannot see: it proves the remote CONTENT
matches local HEAD, and says nothing about ancestry or about how many commits carry it.
Defects 4, 6 and 7 all passed it.

Run: .venv/bin/python tools/push_via_api.py --branch <branch> [--dry-run]
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"

# Modes the tree API accepts for blobs. A gitlink (160000) is a submodule pointer
# and cannot be sent as a blob; refuse rather than push a broken entry.
BLOB_MODES = {"100644", "100755", "120000"}
GITLINK_MODE = "160000"


def parse_raw_diff(raw: bytes) -> list[dict]:
    """Parse `git diff --raw -z -M <base> <head>` into ordered operations.

    The -z raw format is a sequence of NUL-terminated fields:

        :<old_mode> <new_mode> <old_sha> <new_sha> <status>\0<path>\0
        :<old_mode> <new_mode> <old_sha> <new_sha> R<score>\0<old>\0<new>\0

    Rename/copy carry two paths, everything else carries one. Paths are raw bytes
    here on purpose -- -z means git does not quote or escape them, so a path with
    a space, a quote, or 中文 in it survives.
    """
    fields = raw.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    ops: list[dict] = []
    i = 0
    while i < len(fields):
        meta = fields[i]
        if not meta.startswith(b":"):
            raise ValueError(f"expected a raw-diff meta field, got {meta!r}")
        parts = meta[1:].split(b" ")
        if len(parts) != 5:
            raise ValueError(f"malformed raw-diff meta field: {meta!r}")
        old_mode, new_mode, _old_sha, new_sha, status = (p.decode() for p in parts)
        two_paths = status[0] in ("R", "C")
        needed = 3 if two_paths else 2
        if i + needed > len(fields):
            raise ValueError(f"truncated raw diff after {meta!r}")
        if two_paths:
            old_path, path = fields[i + 1], fields[i + 2]
        else:
            old_path, path = None, fields[i + 1]
        ops.append({
            "status": status[0],
            "path": path.decode("utf-8", "surrogateescape"),
            "old_path": old_path.decode("utf-8", "surrogateescape") if old_path else None,
            "old_mode": old_mode,
            "new_mode": new_mode,
            "new_sha": new_sha,
        })
        i += needed
    return ops


def tree_entries(ops: list[dict], upload_blob) -> list[dict]:
    """Turn parsed diff operations into GitHub tree entries.

    `upload_blob(path) -> sha` is called once per path whose content must exist on
    the remote. The mode comes from the op, never from a constant: a mode-only
    change (chmod +x) has identical blob shas on both sides and IS the whole point
    of the entry.
    """
    entries: list[dict] = []
    for op in ops:
        if op["status"] == "D":
            # A null sha is how the tree API expresses "remove this path".
            entries.append({"path": op["path"], "mode": op["old_mode"],
                            "type": "blob", "sha": None})
            continue
        mode = op["new_mode"]
        if mode == GITLINK_MODE:
            raise ValueError(
                f"{op['path']} is a submodule (mode 160000); the tree API cannot "
                "take it as a blob. Push submodule bumps another way.")
        if mode not in BLOB_MODES:
            raise ValueError(f"{op['path']} has unsupported mode {mode}")
        if op["status"] in ("R", "C") and op["old_path"] and op["status"] == "R":
            # A rename is a delete plus an add; the old path would otherwise linger.
            entries.append({"path": op["old_path"], "mode": op["old_mode"],
                            "type": "blob", "sha": None})
        entries.append({"path": op["path"], "mode": mode, "type": "blob",
                        "sha": upload_blob(op["path"])})
    return entries


def git(*args: str, binary: bool = False):
    out = subprocess.run(["git", *args], capture_output=True, check=True)
    return out.stdout if binary else out.stdout.decode().strip()


class GitHub:
    def __init__(self, repo: str, token: str):
        self.repo, self.token = repo, token

    def call(self, path: str, data=None, method: str | None = None,
             absent_ok: bool = False, conflict_ok: bool = False):
        """Call the API. `absent_ok` turns a 404 into None instead of exiting.

        Only the ref lookup passes absent_ok: "this branch does not exist yet" is a
        normal state for the first push of a PR branch, whereas a 404 on a blob or
        tree write is a real failure and must still be fatal.

        `conflict_ok` turns a 422 into None, and only the ref CREATE passes it: a 422
        there means the ref already exists, which contradicts the 404 we based the
        create on and is a recoverable disagreement rather than a failure.
        """
        req = urllib.request.Request(
            f"{API}/repos/{self.repo}{path}",
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json"},
            method=method or ("POST" if data is not None else "GET"))
        try:
            with urllib.request.urlopen(req) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and absent_ok:
                return None
            if exc.code == 422 and conflict_ok:
                return None
            raise SystemExit(f"GitHub {exc.code} on {method or 'GET'} {path}: "
                             f"{exc.read().decode()[:500]}") from exc

    def read_ref(self, branch: str, attempts: int = 4, sleep=time.sleep):
        """Read a branch ref, retrying the 404.

        GET /git/ref is eventually consistent. Believing a single 404 makes the very
        next push after a branch is created decide the branch does not exist -- and
        then base its commit on the default branch, discarding everything already
        pushed. A 404 that is real (a genuinely new branch) costs a few seconds of
        retry once; a 404 that is stale, believed, costs commits.
        """
        for i in range(attempts):
            ref = self.call(f"/git/ref/heads/{branch}", absent_ok=True)
            if ref is not None:
                return ref
            if i + 1 < attempts:
                sleep(2 ** i)
        return None


def drop_already_pushed(gh: "GitHub", locals_: list[str], remote_sha: str) -> list[str]:
    """Drop the leading local commits the remote already has, matched by TREE.

    6. Every already-pushed commit replayed again on the next push. This one was caused
       by the fix for #4. Replaying commit-by-commit means each remote commit is a NEW
       object with a new sha, so the remote branch head is never an ancestor of local
       HEAD -- and `rev-list remote_sha..HEAD` then returns the ENTIRE branch on every
       subsequent push. Observed 2026-08-01 pushing one new commit to
       docs/no-s3-skill-mirror: the plan said "2 commits" and the doc commit landed on
       the remote a second time, with the same message, as a sibling of the first.

       The tree-parity check at the end cannot see this, for the same reason it could not
       see the squash: the final tree is identical whether the prefix is duplicated or
       not. Two different corruptions, one blind spot -- which is why this is matched
       rather than trusted.

    Sha comparison is useless here (the remote shas differ by construction), so the
    remote's recent commits are compared by tree sha. Walking from the oldest local
    commit, a commit whose tree the remote chain already carries has already been pushed;
    the first one that does not is where this push must start. Only a PREFIX is dropped:
    stopping at the first mismatch means a genuinely new commit is never skipped just
    because some later commit happens to restore an earlier tree (a revert does exactly
    that, and dropping it would silently discard the revert).
    """
    seen, sha = set(), remote_sha
    # Bounded: only as far back as the number of commits in play, plus a little slack for
    # the base itself. A long walk here would cost an API call per commit for no gain.
    for _ in range(len(locals_) + 1):
        commit = gh.call(f"/git/commits/{sha}", absent_ok=True)
        if not commit:
            break
        seen.add(commit["tree"]["sha"])
        parents = commit.get("parents") or []
        if not parents:
            break
        sha = parents[0]["sha"]

    keep = list(locals_)
    while keep:
        if git("rev-parse", f"{keep[0]}^{{tree}}") not in seen:
            break
        dropped = keep.pop(0)
        print(f"skipping {dropped[:10]} {git('log', '-1', '--format=%s', dropped)!r} "
              "-- the remote already has this tree")
    if not keep:
        # Everything in the range is already on the remote. Reporting "nothing to push"
        # here would be wrong only if a tree difference remained, and there is none by
        # definition, so the caller's own no-op check handles it.
        return [locals_[-1]]
    return keep


def merge_parents(gh: "GitHub", rev: str) -> tuple[list[str], list[str]]:
    """The parents of `rev` BEYOND THE FIRST, split into (on the remote, not on it).

    The first-parent slot belongs to the commit being advanced onto, and its local sha is
    never the remote's: every remote commit here is API-built, so the two histories are
    parallel by construction (note 2). The caller supplies that slot; sending the local
    first parent alongside it would give every ordinary linear push a bogus second parent.

    A parent the remote cannot resolve is reported and dropped rather than sent -- the API
    rejects an unknown parent sha, and refusing the whole push over a local-only side
    branch is worse than landing it as the linear advance it effectively is. What is
    dropped gets SAID, because a quietly flattened merge is defect 7 itself.
    """
    on_remote, absent = [], []
    for p in git("rev-list", "--parents", "-1", rev).split()[2:]:
        if p in on_remote or p in absent:
            continue
        (on_remote if gh.call(f"/git/commits/{p}", absent_ok=True) is not None
         else absent).append(p)
    return on_remote, absent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--branch", required=True, help="remote branch to advance")
    ap.add_argument("--repo", default=None, help="owner/name (default: gh's view of origin)")
    ap.add_argument("--message", default=None, help="commit message (default: HEAD's)")
    ap.add_argument("--onto", default=None,
                    help="base to replay onto after a local rebase (e.g. the main sha); "
                         "the branch ref is rewritten to the new tip")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, call nothing")
    args = ap.parse_args(argv)

    repo = args.repo or git("config", "--get", "remote.origin.url").rstrip("/") \
        .removesuffix(".git").split("github.com")[-1].lstrip(":/")
    token = subprocess.run(["gh", "auth", "token"], capture_output=True,
                           text=True).stdout.strip()
    if not token:
        raise SystemExit("no token from `gh auth token`")
    gh = GitHub(repo, token)

    ref = gh.read_ref(args.branch)
    creating = ref is None
    if creating:
        # First push of a PR branch. The old code took the 404 as fatal and every new
        # branch had to be conjured by hand with `gh api` before this tool would run --
        # which is exactly the hand-rolled path this tool exists to replace, so the
        # gap put the risky steps back in human hands at the riskiest moment.
        #
        # The base is only the PARENT here; content correctness does not rest on it,
        # because the tree is built by diffing local HEAD against the base and then
        # checked against local HEAD's own tree at the end. So prefer HEAD's parent
        # when the remote already has it (the branch then reads as a child of where it
        # was actually cut) and fall back to the default branch head otherwise.
        parent = git("rev-parse", "HEAD^") if git(
            "rev-list", "--count", "HEAD") != "1" else ""
        if parent and gh.call(f"/git/commits/{parent}", absent_ok=True):
            remote_sha = parent
        else:
            default = gh.call("")["default_branch"]
            remote_sha = gh.call(f"/git/ref/heads/{default}")["object"]["sha"]
            print(f"HEAD's parent is not on the remote; basing {args.branch} on "
                  f"{default} @ {remote_sha[:10]}")
    else:
        remote_sha = ref["object"]["sha"]

    if args.onto:
        # 5. A rebased branch replayed onto its own stale remote head. After a local
        #    rebase the remote head is no longer an ancestor of HEAD, so
        #    `rev-list stale..HEAD` returns this branch's commits AND every commit it
        #    was rebased onto. Observed 2026-08-01 rebasing the round-trip branch onto
        #    main: the plan listed three of main's merge commits ("Merge pull request
        #    #26/#27/#28") for replay as if they were this branch's work -- 3 commits
        #    would have landed as 6, attributing main's merges here. There is no way to
        #    detect this automatically: "the remote head is not an ancestor" is equally
        #    consistent with a rebase and with a divergence that must NOT be discarded.
        #    So the base is stated, and stated bases are verified before anything runs.
        if not gh.call(f"/git/commits/{args.onto}", absent_ok=True):
            raise SystemExit(
                f"--onto {args.onto} is not a commit on {repo}; nothing was pushed. "
                "Pass a sha the remote already has (e.g. `git rev-parse origin/main`).")
        print(f"replaying onto {args.onto[:10]} instead of {args.branch}'s current head "
              f"{remote_sha[:10]} (rebase: the ref will be rewritten)")
        remote_sha = args.onto
    # The remote head must be a local object or the diff base is a guess.
    if subprocess.run(["git", "cat-file", "-e", f"{remote_sha}^{{commit}}"],
                      capture_output=True).returncode != 0:
        # Fetch the sha itself, not args.branch: on a create there is no such remote
        # branch to fetch, and check=True would abort on git's non-zero exit.
        subprocess.run(["git", "fetch", "-q", "origin", remote_sha],
                       capture_output=True)
    if subprocess.run(["git", "cat-file", "-e", f"{remote_sha}^{{commit}}"],
                      capture_output=True).returncode != 0:
        raise SystemExit(f"remote head {remote_sha[:10]} is not in the local object "
                         "store even after a fetch; cannot diff against it")

    head = git("rev-parse", "HEAD")
    # One remote commit per local commit. The old code built a SINGLE commit from one
    # base..HEAD diff and gave it HEAD's message, so pushing two local commits landed
    # as one whose message described only the second -- the rationale for the first was
    # simply gone from the remote, and the tree-parity check below could not see it
    # because the final tree is identical either way. Observed 2026-08-01 pushing the
    # task-chat round-trip work: two commits became `3b62181` carrying the second
    # message, and the reviewer of that PR would never learn why the first change was
    # made. Message loss is not a rendering detail; on a repo where every commit body
    # carries the measurement that justified it, it is the loss of the evidence.
    #
    # --message is an explicit override, so it still squashes: the caller has said what
    # the single resulting commit should say.
    if args.message:
        locals_ = [head]
    else:
        # --first-parent, or a merge puts the MERGED branch's commits in the replay list.
        # `rev-list base..HEAD` walks all ancestry, so merging main in returns main's own
        # commits too and each would be replayed here under its own message as though this
        # branch had authored it. First-parent-only is exactly "the commits this branch
        # advanced by"; what the merge brought in arrives with the merge commit's tree,
        # which is where it belongs. Observed 2026-08-02 on fix/eval-generate-dispatch.
        locals_ = git("rev-list", "--reverse", "--first-parent",
                      f"{remote_sha}..{head}").split()
        if not locals_:
            # HEAD is an ancestor of the remote head (or the same commit). There may
            # still be a tree difference if the remote was built by an earlier squash,
            # so fall back to one commit rather than reporting nothing to do.
            locals_ = [head]
        else:
            locals_ = drop_already_pushed(gh, locals_, remote_sha)

    plan_ops, plan_merges = {}, {}
    prev = remote_sha
    for c in locals_:
        plan_ops[c] = parse_raw_diff(git("diff", "--raw", "-z", "-M", prev, c,
                                         binary=True))
        # A merge is squashed by --message, so its extra parents would point at commits
        # this squashed commit does not represent; only a real replay carries them.
        plan_merges[c], absent = ([], []) if args.message else merge_parents(gh, c)
        if absent:
            print(f"NOTE: {c[:10]} has parent(s) {[p[:10] for p in absent]} the remote "
                  "does not have; pushing without them. It will read as a linear commit.",
                  file=sys.stderr)
        prev = c
    if not any(plan_ops.values()) and not any(plan_merges.values()):
        # The merge clause is not redundant: merging a branch already contained changes no
        # file, and what that commit carries is the ANCESTRY, not the tree. Returning here
        # would leave the remote at a commit the merged branch is not an ancestor of --
        # defect 7's wrong ancestry again, reached by an early return instead of a flatten.
        print(f"remote {args.branch} already matches HEAD tree; nothing to push")
        return 0

    verb = "create from" if creating else "advance"
    print(f"{repo} {args.branch}: {verb} {remote_sha[:10]} -> tree of {head[:10]} "
          f"({len(locals_)} commit{'s' if len(locals_) != 1 else ''})")
    for c in locals_:
        subject = git("log", "-1", "--format=%s", c)
        merged = f" +merge {[p[:10] for p in plan_merges[c]]}" if plan_merges[c] else ""
        print(f"  {c[:10]} {subject}{merged}")
        for op in plan_ops[c]:
            moved = f" (was {op['old_path']})" if op["old_path"] else ""
            chmod = f" mode {op['old_mode']}->{op['new_mode']}" \
                if op["old_mode"] != op["new_mode"] and op["status"] != "D" else ""
            print(f"    {op['status']} {op['path']}{moved}{chmod}")
    if args.dry_run:
        return 0

    def uploader(commit_ish: str):
        def upload_blob(path: str) -> str:
            # Content comes from the commit being replayed, not from HEAD: a file that
            # changed twice would otherwise have HEAD's content in the FIRST commit,
            # making the intermediate commit a lie about what it did.
            content = git("show", f"{commit_ish}:{path}", binary=True)
            return gh.call("/git/blobs", {
                "content": base64.b64encode(content).decode(),
                "encoding": "base64"})["sha"]
        return upload_blob

    parent_sha, tree = remote_sha, None
    for c in locals_:
        entries = tree_entries(plan_ops[c], uploader(c))
        base_tree = gh.call(f"/git/commits/{parent_sha}")["tree"]["sha"]
        tree = gh.call("/git/trees", {"base_tree": base_tree,
                                      "tree": entries})["sha"] if entries else base_tree
        message = args.message or git("log", "-1", "--format=%B", c)
        commit = gh.call("/git/commits", {
            "message": message, "tree": tree,
            "parents": [parent_sha, *plan_merges[c]]})
        parent_sha = commit["sha"]
    if creating:
        # POST /git/refs with a full ref name creates; PATCH would 404 on a ref that
        # does not exist yet, which is the whole reason this branch of the code exists.
        if gh.call("/git/refs", {"ref": f"refs/heads/{args.branch}",
                                 "sha": parent_sha}, conflict_ok=True) is None:
            # 422: the ref exists after all, so the 404 that put us here was stale and
            # the commit just built is parented on the wrong base. Refuse rather than
            # PATCH -- pointing the branch at this commit would drop every commit
            # already on it. Re-running now succeeds, because the ref read will find it.
            raise SystemExit(
                f"{args.branch} already exists on the remote, but the ref read said it "
                f"did not, so commit {parent_sha[:10]} was built on the wrong base "
                f"({remote_sha[:10]}). Nothing was moved -- that commit is unreferenced "
                "and harmless. Re-run this command; the ref is visible now.")
    else:
        # force ONLY on --onto. A rebase moves the branch to a commit that is not a
        # descendant of the old head, so the PATCH is not a fast forward and GitHub
        # answers 422 (observed live 2026-08-01). Everywhere else the same 422 means
        # something else entirely -- the remote has commits this run never saw -- and
        # forcing there would delete them. So the force is tied to the flag that says
        # "the ref is being rewritten on purpose", not applied as a general fix.
        body = {"sha": parent_sha}
        if args.onto:
            body["force"] = True
        gh.call(f"/git/refs/heads/{args.branch}", body, method="PATCH")

    local_tree = git("rev-parse", f"{head}^{{tree}}")
    if tree != local_tree:
        print(f"WARNING: pushed tree {tree[:10]} != local tree {local_tree[:10]}; "
              "the remote does not match your working commit", file=sys.stderr)
        return 1
    print(f"pushed {parent_sha[:10]}; tree matches local HEAD exactly "
          f"({len(locals_)} commit{'s' if len(locals_) != 1 else ''})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
