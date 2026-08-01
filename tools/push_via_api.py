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

So this reads modes from git instead of assuming them, diffs against the branch's
actual remote head instead of assuming the local parent matches it, and never
concludes "no such branch" from a single 404. A rename becomes delete-old + add-new;
a deletion becomes a null-sha tree entry.

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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--branch", required=True, help="remote branch to advance")
    ap.add_argument("--repo", default=None, help="owner/name (default: gh's view of origin)")
    ap.add_argument("--message", default=None, help="commit message (default: HEAD's)")
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
    ops = parse_raw_diff(git("diff", "--raw", "-z", "-M", remote_sha, head, binary=True))
    if not ops:
        print(f"remote {args.branch} already matches HEAD tree; nothing to push")
        return 0

    verb = "create from" if creating else "advance"
    print(f"{repo} {args.branch}: {verb} {remote_sha[:10]} -> tree of {head[:10]}")
    for op in ops:
        moved = f" (was {op['old_path']})" if op["old_path"] else ""
        chmod = f" mode {op['old_mode']}->{op['new_mode']}" \
            if op["old_mode"] != op["new_mode"] and op["status"] != "D" else ""
        print(f"  {op['status']} {op['path']}{moved}{chmod}")
    if args.dry_run:
        return 0

    def upload_blob(path: str) -> str:
        content = git("show", f"{head}:{path}", binary=True)
        return gh.call("/git/blobs", {
            "content": base64.b64encode(content).decode(), "encoding": "base64"})["sha"]

    entries = tree_entries(ops, upload_blob)
    base_tree = gh.call(f"/git/commits/{remote_sha}")["tree"]["sha"]
    tree = gh.call("/git/trees", {"base_tree": base_tree, "tree": entries})["sha"]
    message = args.message or git("log", "-1", "--format=%B")
    commit = gh.call("/git/commits", {"message": message, "tree": tree,
                                     "parents": [remote_sha]})
    if creating:
        # POST /git/refs with a full ref name creates; PATCH would 404 on a ref that
        # does not exist yet, which is the whole reason this branch of the code exists.
        if gh.call("/git/refs", {"ref": f"refs/heads/{args.branch}",
                                 "sha": commit["sha"]}, conflict_ok=True) is None:
            # 422: the ref exists after all, so the 404 that put us here was stale and
            # the commit just built is parented on the wrong base. Refuse rather than
            # PATCH -- pointing the branch at this commit would drop every commit
            # already on it. Re-running now succeeds, because the ref read will find it.
            raise SystemExit(
                f"{args.branch} already exists on the remote, but the ref read said it "
                f"did not, so commit {commit['sha'][:10]} was built on the wrong base "
                f"({remote_sha[:10]}). Nothing was moved -- that commit is unreferenced "
                "and harmless. Re-run this command; the ref is visible now.")
    else:
        gh.call(f"/git/refs/heads/{args.branch}", {"sha": commit["sha"]},
                method="PATCH")

    local_tree = git("rev-parse", f"{head}^{{tree}}")
    if tree != local_tree:
        print(f"WARNING: pushed tree {tree[:10]} != local tree {local_tree[:10]}; "
              "the remote does not match your working commit", file=sys.stderr)
        return 1
    print(f"pushed {commit['sha'][:10]}; tree matches local HEAD exactly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
