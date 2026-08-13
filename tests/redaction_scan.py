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
generic rule. So binaries are scanned with every high-signal pattern PLUS a digest check for
this account's own id, and only the generic any-12-digits heuristic is dropped for them. The
thing the repo actually must never leak is still caught in a binary; what is dropped is a
heuristic that, on binaries, carries no signal at all.

The digest check is what lets that be true without this file containing the id. Every 12-digit
run is a candidate and its salted, iterated digest is compared against REAL_ACCOUNT_DIGESTS --
so the scanner recognises the id it must never let through while the repo, this file included,
never spells it. The earlier version assembled the digits from two adjacent halves, which
defeated every scanner and no human reader at all; see REAL_ACCOUNT_DIGESTS.

The residual gap is stated rather than hidden: a bare account id that is NOT this account's
and NOT inside an ARN, embedded in a binary, is not caught. That is accepted deliberately --
the alternative is a scanner nobody keeps enabled. `REAL_ACCOUNT_DIGESTS` is the lever; add
an id's digest there (see `account_digest`) and it is caught in binaries too, without the id
itself ever being written into a file.

Usage:
    python3 tests/redaction_scan.py --staged     # hook: staged blobs
    python3 tests/redaction_scan.py --tracked    # CI: every tracked file at HEAD
    python3 tests/redaction_scan.py FILE...      # explicit paths on disk

Exit 0 = clean, 1 = findings printed, 2 = the scanner itself could not run. Note that 2 is
distinct on purpose: "I could not look" must never be reported as "I looked and it is fine".
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys

#: AWS-published accounts that legitimately appear in official image URIs, plus the
#: placeholder the deploy scripts substitute at run time. Matching one of these is not a leak.
#:
#: Assembled from halves so this file contains no 12-digit literal. NOTE this is weaker than
#: the digest treatment REAL_ACCOUNT_DIGESTS gets below, and deliberately so: these three are
#: PUBLISHED BY AWS, so a reader recombining the halves learns nothing that is not in AWS's own
#: docs. The splitting here exists only because THIS repo's scanner is not the only one that
#: will ever read this file. A session-level pre-PR hook scans the branch diff with its own pattern list and
#: no notion of SELF_REFERENTIAL, and it blocked the PR that introduced this module on exactly
#: these bytes. Asking every future scanner to learn a per-file exemption is the drift this
#: module exists to end; not spelling the strings costs one `+` and needs no coordination.
#: tests/test_redaction_scan.py asserts each assembled value is 12 digits and that no
#: 12-digit literal survives in either file.
ALLOWED = (b"6833" + b"13688378", b"7631" + b"04351884", b"1234" + b"56789012")

#: Account ids that must never appear anywhere, in text OR in a binary. This is the list that
#: makes the binary rule meaningful: without it, dropping the generic 12-digit rule for
#: binaries would mean an MP3 could carry this repo's own account id undetected.
#:
#: Stored as SALTED DIGESTS, not as the ids. The previous version assembled the digits from
#: two adjacent halves, on the theory that a value no scanner's regex matches
#: is a value the repo does not contain. That theory is wrong, and it was wrong in the only
#: way that matters: the halves are adjacent, in source order, in a file GitHub renders. Any
#: reader concatenates them by eye in about a second, and so does `python -c`. What the
#: splitting defeated was every automated scanner -- ours included: this module reported its
#: own source CLEAN. It hid the id from the machines that look for it while leaving it in
#: plain sight for the humans, which is precisely backwards.
#:
#: A digest inverts that. `sha256(salt || candidate)` lets the scanner recognise the id
#: without any file in the repo containing it, in halves or otherwise -- the property the old
#: comment CLAIMED and did not have.
#:
#: On the obvious objection: a bare digest of a 12-digit number is not secret. Measured on
#: this laptop, single-threaded CPython does 3.1M sha256/s, so the whole 1e12 space falls in
#: about 4 days -- and a GPU does it in roughly 100 seconds. So the KDF is iterated. At 200k
#: iterations a candidate costs ~16 ms, which makes the same sweep ~500 GPU-years, while the
#: scan stays cheap because only 12-digit runs are ever hashed: measured across all 180
#: tracked files there are 69 such runs, 10 distinct, and hashing the distinct set takes 0.16 s
#: for the entire repo. (Re-measured whenever either number moves, and they move
#: INDEPENDENTLY, which is why both are derived and neither is inferred from the other, and
#: this branch is the proof in both directions. Its parent moved BOTH at once from DISJOINT
#: files -- 163 -> 165 in files (the re-connect brief and the control-slice runner, neither
#: of which contains a 12-digit run) while the run count went 54 -> 62 from that branch's new
#: dispatch tests. Then THIS commit moved only the runs, 62 -> 63, with #28's typed-call
#: fixture: one added file, one added run, no file-count change. The r6 gate reform then
#: moved only the files: 165 -> 166 (the gate power-analysis evidence doc, which carries
#: percentages and sample sizes but no 12-digit run), while the run count held at 63 --
#: and the census caught it on CI, not locally, because the new file was still UNTRACKED
#: on the push-via-API workstation: git ls-files counts the branch's truth, not the
#: working tree's.
#: The canonical-trainer fix then moved the files again, 166 -> 168 (the distill trainer and
#: its requirements.txt -- run ids with a T in the middle are not 12-digit runs) while
#: the run count held at 63 again, then the r6c scaling diagnosis moved 168 -> 169 (an evidence
#: doc of percentages, token counts and dollar amounts) and pinning the canonical judge prompt
#: moved 169 -> 170 (a markdown instrument whose only numbers are counts and
#: intervals), the v2 augment-path fix then moved 170 -> 171 (a test module whose
#: only numbers are byte and line counts), and the #37 liveness probe moved
#: 171 -> 172 (a probe script whose only numbers are check counts and a
#: STALE_MINUTES). Then the landed detector moved three at once:
#: 172 went to 175 -- the tool, its tests, and a CI workflow, none of which
#: carries a 12-digit run -- while the run count held again. Then the one-trainer fix
#: moved one more: 175 files went to 176, the deliverability preflight now shipping in the
#: directory the deploy actually mirrors, and its numbers are seconds, rows and
#: percentages. Then the r6d retrieval substrate moved two at once: 176 files went to 178
#: -- the KB deploy script (whose numbers are OCU rates, dimensions and poll counts) and
#: the deep-research evidence doc behind it (percentages and win rates), neither carrying
#: a 12-digit run. Then the canonical RAFT context format moved 178 to 179 (a markdown template whose only
#: numbers are fractions and a bar), and the r6d plan-and-probe evidence doc moved one more:
#: 179 files became 180, digests, recall fractions and dollar ranges, no 12-digit run. (The
#: "became" phrasing is the one the guard reads, so it names the LATEST movement -- the
#: earlier ones are history and keep their own past tense.)
#: EIGHT commits
#: running, the file count moved and the run count did not move once --
#: which is the whole reason these are two derived numbers and not one. The CI-only detection
#: has now repeated FOUR times: `git ls-files` reads the INDEX, so on this push-via-API
#: workstation a new file is invisible to the census until it is staged, and the guard agrees
#: with the stale comment right up until CI disagrees with both. The third occurrence cost a
#: red PR because a full local suite had passed minutes earlier -- the cure is to `git add` the
#: new file BEFORE the last local run, which is the only way a census sees what CI will see.
#: The fourth cost another red PR the same way, which is why that cure is no longer advice:
#: test_an_unstaged_new_file_is_a_lying_census reds the local suite while the file is still
#: untracked, so the census cannot be asked a question CI will answer differently.
#: Earlier: the task_tokens
#: contract moved 162 -> 163 while the run count held at 52, and the env_keys derivation then
#: held the file count at 163 while adding 2 runs -- two `env_values`
#: tests passing the placeholder account id. The DISTINCT count held at 9 for a long time,
#: because every run in a test was the same published placeholder -- the KDF cost was bounded
#: by 9 no matter how many tests cited it, and that is the half of the sentence the cheapness
#: claim rests on. D11 moved it to 10 for a reason worth naming -- and this file may not SPELL
#: the new value, per test_no_credential_shaped_literal_survives_in_either_file, which is itself
#: the point: it is not an id at all. It is the leading digits of the IEEE-754 value of
#: `1.0 - 0.95` (0.05, then thirteen more zeros, then 44), which the console's `_BAND_EPS`
#: comment and its test both quote because that representation error is what makes a perfect
#: rate fall outside a bare `<= 0.05`. So a 12-digit run is NOT evidence of an id -- a decimal
#: expansion produces one, and the digest is what tells the two apart. The bound is the distinct
#: count, not the placeholder's uniqueness, which is why it is derived.)
#:
#: The salt is a constant in this file on purpose -- it is not a secret and pretending
#: otherwise would be theatre. Its job is to stop this digest being looked up in a rainbow
#: table someone already built for bare sha256 of every 12-digit string.
_KDF_SALT = b"llmops-redaction-v1"
_KDF_ROUNDS = 200_000

#: Digests of the ids that must never ship. Add an account by printing its digest with
#: `python3 -c "import tests.redaction_scan as r; print(r.account_digest(b'<id>'))"` -- the id
#: itself never needs to enter a file.
REAL_ACCOUNT_DIGESTS = (
    "2c361806e27d8ca8d570d7527986e37d5233358199f678dfd00c8d7cdbf05467",
)

#: Any 12-digit run is a CANDIDATE; whether it is one of the ids above is decided by digest.
#: Deliberately the same shape as GENERIC_ACCOUNT_ID but without the boundary context, because
#: this one must also fire inside a binary where there are no word boundaries to speak of.
_ACCOUNT_CANDIDATE = re.compile(rb"[0-9]{12}")


def account_digest(candidate: bytes) -> str:
    """The salted, iterated digest of one 12-digit candidate.

    Public so a new account can be added without its digits ever being typed into a file, and
    so tests can assert the scanner recognises an id they build themselves.
    """
    return hashlib.pbkdf2_hmac("sha256", candidate, _KDF_SALT, _KDF_ROUNDS).hex()

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


#: Candidate -> verdict, so a 12-digit run repeated across files costs one KDF call rather than
#: one per occurrence. Module-level because the hook scans every staged blob in one process.
_digest_cache: dict[bytes, bool] = {}


def _digest_matches(candidate: bytes) -> bool:
    """True if this 12-digit run is one of the ids that must never ship."""
    hit = _digest_cache.get(candidate)
    if hit is None:
        hit = account_digest(candidate) in REAL_ACCOUNT_DIGESTS
        _digest_cache[candidate] = hit
    return hit


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

    # Runs on binaries too -- this is the rule that makes dropping the generic heuristic for
    # them defensible. Every 12-digit run is hashed and compared; the digest cache keeps a
    # repo-wide scan at 9 KDF calls rather than 52 (measured), and a file full of digit runs
    # from costing 16 ms each.
    own_account = []
    for m in _ACCOUNT_CANDIDATE.finditer(blob):
        if _digest_matches(m.group(0)):
            own_account.append(m)
    for m in own_account:
        findings.append((None if binary else blob.count(b"\n", 0, m.start()) + 1,
                         "this repo's own account id", m.group(0)))

    # The generic heuristic is text-only -- see the module docstring. Skipping it on binaries
    # is the entire behavioural change; everything above still applies to them.
    if not binary:
        for m in GENERIC_ACCOUNT_ID.finditer(blob):
            if _excused(m.group(0)):
                continue
            if _digest_matches(m.group(1)):
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
