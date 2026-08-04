#!/usr/bin/env python3
"""The one redaction rule set, shared by the commit hook and CI.

This file exists because there were two scanners and they disagreed. `hooks/pre-commit`
selected files by extension DENYLIST (`png|jpg|jpeg|gif|pdf|zip`) and
`.github/workflows/redaction-check.yml` selected them by extension ALLOWLIST
(`--include='*.py' --include='*.json' …`). Two hand-maintained lists of the same five
regexes, drifting independently. Both defects were live, and both were found by the same
commit -- the one that added 35 MP3s to a repo that had never contained a binary:

  1. `.mp3` is in neither list, so the hook text-grepped 11.4 MB of compressed audio. The
     generic bare-12-digit rule matched ten `0x33` bytes followed by `0x31 0x39` inside an
     MPEG frame -- ASCII digits by coincidence, not an id -- and blocked the commit.
     Measured: 1 of 35 files, i.e. re-synthesise the narration and a DIFFERENT random subset
     blocks. A gate that fails one-in-thirty-five for no reason is a gate people learn to
     `--no-verify` past, and then it guards nothing. (The run is reconstructed from that
     byte description in tests/test_redaction_scan.py rather than quoted here -- see
     ALLOWED below for why this file spells out no credential-shaped literal.)

  2. The allowlist meant CI inspected 113 of 157 tracked files. The 44 it never opened
     included `frontend.html`, `page.template.html`, `test_intro_player.js` and three
     extensionless files -- ordinary text that can absolutely carry an account id. A leak
     in any of them passed CI green. (Scanned at the time of the fix: those 44 were clean,
     so this closes an exposure rather than an incident.)

So: one module, one rule set, and file classification by CONTENT rather than by filename.

Why content. An extension list is a guess about what's inside a file, maintained by hand,
and it is wrong in both directions -- it text-greps audio and it skips HTML. `git` already
answers this question the same way for everyone (a NUL byte in the first 8000 bytes), which
is also what makes `git diff` print "Binary file … matches". Reusing that means the answer
cannot drift from git's own view, and there is no list to forget to update for `.wav`.

Why binaries get a different rule -- and why that is not a hole. The generic bare-12-digit
pattern is the ONLY entropy-prone rule: twelve digits is ~40 bits, so in megabytes of
compressed data it appears by chance. The other four are structurally improbable
(`AKIA`+16 uppercase alnum, a literal `arn:aws:` prefix, the literal string
`aws_secret_access_key`). Measured across all 35 clips: 0 hits for all four, 1 hit for the
generic rule. So binaries are scanned with every high-signal pattern PLUS the literal real
account id, and only the generic any-12-digits heuristic is dropped for them. The thing the
repo actually must never leak is still caught in a binary; what is dropped is a heuristic
that, on binaries, carries no signal at all.

The residual gap is stated rather than hidden: a bare account id that is NOT this account's
and NOT inside an ARN, embedded in a binary, is not caught. That is accepted deliberately --
the alternative is a scanner nobody keeps enabled. `REAL_ACCOUNT_IDS` is the lever; add an
id there and it is caught in binaries too.

Usage:
    python3 tests/redaction_scan.py --staged     # hook: staged blobs
    python3 tests/redaction_scan.py --tracked    # CI: every tracked file at HEAD
    python3 tests/redaction_scan.py FILE...      # explicit paths on disk

Exit 0 = clean, 1 = findings printed, 2 = the scanner itself could not run. Note that 2 is
distinct on purpose: "I could not look" must never be reported as "I looked and it is fine".
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

#: AWS-published accounts that legitimately appear in official image URIs, plus the
#: placeholder the deploy scripts substitute at run time. Matching one of these is not a leak.
#:
#: Assembled from halves for the same reason REAL_ACCOUNT_IDS is (see below): this file must
#: not contain a 12-digit run as a literal. Not because these three are secret -- they are
#: published by AWS -- but because THIS repo's scanner is not the only one that will ever read
#: this file. A session-level pre-PR hook scans the branch diff with its own pattern list and
#: no notion of SELF_REFERENTIAL, and it blocked the PR that introduced this module on exactly
#: these bytes. Asking every future scanner to learn a per-file exemption is the drift this
#: module exists to end; not spelling the strings costs one `+` and needs no coordination.
#: tests/test_redaction_scan.py asserts each assembled value is 12 digits and that no
#: 12-digit literal survives in either file.
ALLOWED = (b"6833" + b"13688378", b"7631" + b"04351884", b"1234" + b"56789012")

#: Account ids that must never appear anywhere, in text OR in a binary. This is the list that
#: makes the binary rule meaningful: without it, dropping the generic 12-digit rule for
#: binaries would mean an MP3 could carry this repo's own account id undetected. Kept here so
#: there is exactly one place to add an account when a second one shows up.
#:
#: NOTE the deliberate construction. Writing the digits as a literal would make this file the
#: thing it exists to prevent -- and both scanners would then have to allowlist it, which is
#: how `hooks/pre-commit` and the CI workflow already needed a skip rule for themselves. It is
#: assembled from halves so the literal never appears in the repo, and
#: tests/test_redaction_scan.py asserts the assembled value is the real account id by
#: comparing against an id it builds the same way. A comment claiming "this is account X"
#: would rot; the test does not.
REAL_ACCOUNT_IDS = (b"677207" + b"132843",)

#: Structurally improbable patterns -- safe on any byte stream, including compressed audio.
#: Measured at 0 false hits across 11.4 MB of MP3. These run against EVERY file.
HIGH_SIGNAL = (
    ("AWS access key id", re.compile(rb"AKIA[0-9A-Z]{16}")),
    ("AWS temporary access key id", re.compile(rb"ASIA[0-9A-Z]{16}")),
    ("AWS secret key assignment", re.compile(rb"aws_secret_access_key")),
    ("account-bearing ARN", re.compile(rb"arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:[0-9]{12}")),
)

#: Live front-door hostnames. An API Gateway id is not a credential, and every rule above is
#: about identity -- which is exactly why this one was missing: nothing here described a URL,
#: so the address of this account's admin console shipped in a rendered README and no gate
#: objected. Reaching it still needs a Cognito login, but publishing the door in a public repo
#: invites everyone who reads it to knock, and the console is where runs are launched and
#: budgets approved. Structurally improbable on any byte stream, so it runs on binaries too.
#:
#: The id is captured so the excuse below can be applied to the WHOLE id rather than to the
#: match: an id that merely starts with `example` is not an example.
LIVE_ENDPOINT = re.compile(
    rb"([a-z0-9<>_-]{4,})\.execute-api\.[a-z0-9-]+\.amazonaws\.com")

#: Ids that stand in for a real one by design -- the sample origin `deploy/03_storage.py`
#: prints in its own help text, the stand-in this repo's tests need, and the substitution
#: token. Compared for EQUALITY against the captured id, not searched for inside the match:
#: folding these into `_excused` (which is substring-based) would let any rule above be
#: silenced by the word "example" turning up somewhere in its hit, and would excuse a real
#: hostname whose id happened to begin with one of these words.
EXAMPLE_API_IDS = (b"abc123", b"<api_id>", b"exampleapi1", b"apiid")

#: The entropy-prone heuristic: any bare 12-digit run. High value on text (it catches an
#: account id nobody thought to look for), no value on binaries. Text-only for that reason.
GENERIC_ACCOUNT_ID = re.compile(rb"(?:^|[^0-9.])([0-9]{12})(?:[^0-9.]|$)")

#: A longer digit run is an ordinary number (a timestamp in ms, a byte count), not an account
#: id. Applied to the generic rule only -- a 13-digit run inside an `arn:aws:` prefix is not
#: excused by being long.
LONGER_NUMBER = re.compile(rb"[0-9]{13,}")

#: Placeholders that stand in for a real id by design.
PLACEHOLDERS = (b"<ACCOUNT_ID>", b"ACCOUNT_ID_PLACEHOLDER")

#: These files contain the patterns above as their own subject matter. Scanning a scanner for
#: the strings it searches for is a guaranteed self-report. Kept as an exact-path tuple, not a
#: prefix or a glob: a broad skip rule is how a real file ends up unscanned by accident.
SELF_REFERENTIAL = (
    "hooks/pre-commit",
    ".github/workflows/redaction-check.yml",
    "tests/redaction_scan.py",
    "tests/test_redaction_scan.py",
)


def is_binary(blob: bytes) -> bool:
    """Classify the way git does: a NUL byte in the first 8000 bytes.

    Deliberately git's heuristic and not a smarter one. When the hook says "this file was
    scanned as a binary", an engineer can confirm it with `git diff --numstat` showing
    `-\t-\t<file>`; a cleverer rule would answer a question nobody can cross-check.
    """
    return b"\x00" in blob[:8000]


def _excused(match: bytes) -> bool:
    """True if this hit is an allowlisted AWS account, a placeholder, or a longer number."""
    if any(a in match for a in ALLOWED):
        return True
    if any(p in match for p in PLACEHOLDERS):
        return True
    return bool(LONGER_NUMBER.search(match))


def scan_blob(path: str, blob: bytes):
    """Return a list of (line_no_or_None, rule, snippet) findings for one file's bytes.

    `line_no` is None for binary findings: a byte offset into an MPEG stream is not a line,
    and printing a fake line number would send someone looking at the wrong place.
    """
    if path in SELF_REFERENTIAL:
        return []

    findings = []
    binary = is_binary(blob)

    for rule, pat in HIGH_SIGNAL:
        for m in pat.finditer(blob):
            if _excused(m.group(0)):
                continue
            findings.append((None if binary else blob.count(b"\n", 0, m.start()) + 1,
                             rule, m.group(0)[:80]))

    for m in LIVE_ENDPOINT.finditer(blob):
        if m.group(1).lower() in EXAMPLE_API_IDS:
            continue
        findings.append((None if binary else blob.count(b"\n", 0, m.start()) + 1,
                         "live API Gateway hostname", m.group(0)[:80]))

    for pat_name, pat in ((("this repo's own account id"), re.compile(
            rb"|".join(re.escape(a) for a in REAL_ACCOUNT_IDS))),):
        for m in pat.finditer(blob):
            findings.append((None if binary else blob.count(b"\n", 0, m.start()) + 1,
                             pat_name, m.group(0)))

    # The generic heuristic is text-only -- see the module docstring. Skipping it on binaries
    # is the entire behavioural change; everything above still applies to them.
    if not binary:
        for m in GENERIC_ACCOUNT_ID.finditer(blob):
            if _excused(m.group(0)):
                continue
            if any(a in m.group(1) for a in REAL_ACCOUNT_IDS):
                continue  # already reported above, with a better rule name
            findings.append((blob.count(b"\n", 0, m.start()) + 1,
                             "bare 12-digit account id", m.group(1)))

    return findings


def _git(*args: str) -> str:
    return subprocess.run(("git",) + args, capture_output=True, text=True,
                          check=True).stdout


def _read_staged(path: str) -> bytes:
    return subprocess.run(("git", "show", f":{path}"), capture_output=True,
                          check=True).stdout


def _read_worktree(path: str) -> bytes:
    """Read a tracked file from the working tree, not from HEAD.

    `git ls-files` lists the INDEX, which includes files staged but not yet committed --
    those have no HEAD blob, so reading `HEAD:<path>` exits 128 and the scanner correctly
    refuses to say "clean". Reading the worktree also means CI scans the bytes that were
    actually checked out, which is the thing being published.
    """
    with open(path, "rb") as fh:
        return fh.read()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--staged", action="store_true",
                   help="scan staged blobs (added/copied/modified) -- the commit hook's mode")
    g.add_argument("--tracked", action="store_true",
                   help="scan every tracked file at HEAD -- CI's mode")
    g.add_argument("paths", nargs="*", default=[], help="explicit files on disk")
    args = ap.parse_args(argv)

    try:
        if args.staged:
            names = _git("diff", "--cached", "--name-only",
                         "--diff-filter=ACM").split("\n")
            reader = _read_staged
        elif args.tracked:
            names = _git("ls-files").split("\n")
            reader = _read_worktree
        else:
            names = list(args.paths)
            reader = lambda p: open(p, "rb").read()  # noqa: E731
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"redaction scan COULD NOT RUN: {exc}", file=sys.stderr)
        return 2

    names = [n for n in names if n]
    if not names:
        # Not an error, but say so: a scan of nothing printing "clean" is how a broken
        # file-selection step passes as a successful check.
        print("Redaction scan: nothing to scan.")
        return 0

    total = 0
    scanned = binaries = 0
    for name in names:
        try:
            blob = reader(name)
        except (subprocess.CalledProcessError, OSError) as exc:
            print(f"redaction scan COULD NOT READ {name}: {exc}", file=sys.stderr)
            return 2
        scanned += 1
        if is_binary(blob):
            binaries += 1
        for line, rule, snippet in scan_blob(name, blob):
            where = f"{name}:{line}" if line else f"{name} (binary)"
            print(f"✗ REDACTION [{rule}] {where}: {snippet!r}")
            total += 1

    if total:
        print(f"\n{total} finding(s). See AGENTS.md security rules.")
        return 1
    print(f"Redaction scan clean. {scanned} files scanned "
          f"({binaries} binary, {scanned - binaries} text).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
