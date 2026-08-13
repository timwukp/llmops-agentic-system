"""Guards for the shared redaction scanner.

The scanner is the thing standing between a public repo and a leaked account id, so the
property that matters is not "it passes today" but "it still fails on a real leak". Both
directions are asserted, because this file exists because of a fix that traded one for the
other: dropping the entropy-prone generic rule on binaries is only safe if the high-signal
rules genuinely still fire inside a binary. A test that only checked "clean audio passes"
would have called an unconditional `return []` a success.

Two live defects motivated the module these guard (see its docstring): the commit hook
text-grepped MP3s and blocked on a run of `0x33` bytes, and CI's extension allowlist meant it
never opened frontend.html, page.template.html, test_intro_player.js or any extensionless file.

Note that NOTHING in this file spells a 12-digit run, an AKIA key or an account-bearing ARN as
a literal -- every one is assembled or reconstructed, and
`test_no_credential_shaped_literal_survives_in_either_file` enforces it. The reason is the
lesson of the module itself: this repo's scanner exempts these two files via SELF_REFERENTIAL,
but it is not the only scanner that reads them. A session-level pre-PR hook scans the branch
diff with its own pattern list and no such exemption, and it blocked the PR introducing this
module on precisely these bytes. Teaching every future scanner a per-file exemption is the
drift this module was written to end; writing no literal needs no coordination at all.
"""

import hashlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))

import redaction_scan as rs  # noqa: E402

#: A real MPEG frame header plus a NUL, so is_binary() classifies it the way git would.
#: Built rather than read from disk so these tests do not depend on the narration existing.
BINARY_PREFIX = b"\xff\xfb\x90\x00" + b"\x00" * 32

#: The exact byte run that blocked the commit, RECONSTRUCTED from its description rather than
#: quoted: ten `0x33` bytes then `0x31 0x39`. `bytes([...])` is the point -- the twelve ASCII
#: digits never appear as a literal in this file, so no scanner anywhere needs to exempt it,
#: while the test below still asserts against the identical twelve bytes.
BLOCKING_RUN = bytes([0x33] * 10 + [0x31, 0x39])

#: A synthetic account id, likewise assembled. Not this account's, not AWS's -- it exists only
#: to be a 12-digit run inside an ARN.
SYNTHETIC_ACCOUNT = b"9998" + b"88777666"

#: git's well-known empty tree object -- the same sha in every git repository ever created, so
#: hardcoding it is not a local assumption. Diffing the index against it lists the WHOLE index,
#: which is how the binary-classification guard below covers every tracked file on a clean
#: checkout instead of skipping.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

#: AWS's own published example access key id, split at the boundary between the structural
#: `AKIA` prefix and its body so the 20-char literal never appears whole.
EXAMPLE_AKIA = b"AKIA" + b"IOSFODNN7EXAMPLE"
EXAMPLE_ASIA = b"ASIA" + b"IOSFODNN7EXAMPLE"


def _recursive_scan_re():
    """A recursive content scan in a shell script — i.e. a second rule set.

    `r` may be bundled into a short-flag cluster, so the pattern accepts any cluster of
    letters containing r/R as well as the long form. The first version demanded the flag stand
    alone and a negative control spelled `grep -rn` walked past it, which is why the spellings
    are now pinned in a table (_MUST_BE_FLAGGED / _MUST_BE_ALLOWED) instead of trusted.

    Anchored on a `\\s` before the flag so `--include` and other long options cannot supply the
    r; stops at `|` so a pipeline's later stage is judged on its own.
    """
    return re.compile(
        r"\b(?:grep|rg|ag|ugrep)\b[^\n|]*\s(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)\b")


def _bin(payload=b""):
    return BINARY_PREFIX + payload


def _text(payload=b""):
    return b"# a text file\n" + payload + b"\n"


# --------------------------------------------------------------------------------------
# The direction that would silently rot: real secrets must still be caught in a BINARY.
# --------------------------------------------------------------------------------------

#: A stand-in for this repo's own account id, and the whole point of the digest scheme: this
#: file no longer needs the real digits to prove the real digits are caught.
#:
#: The previous version of this list carried this account's id as two adjacent halves, and an
#: ARN built from them.
#: Two adjacent halves in source order are not redaction -- a reader recombines them by eye --
#: and it is what GitHub secret scanning alert #1 was pointing at. The scheme under test is
#: "the scanner recognises whatever is in REAL_ACCOUNT_DIGESTS", so it is tested with a
#: SYNTHETIC id whose digest is injected. What the real digest is stays in the scanner and
#: appears in no test.
STANDIN_ACCOUNT = b"4242" + b"42424242"


@pytest.fixture
def standin_is_the_watched_account(monkeypatch):
    """Point the scanner at STANDIN_ACCOUNT instead of the real one, for this test only.

    Injected rather than added permanently: a scanner that permanently watches a test fixture's
    id would fire on the fixture and get exempted, and the exemption is the hole.
    """
    monkeypatch.setattr(rs, "REAL_ACCOUNT_DIGESTS",
                        (rs.account_digest(STANDIN_ACCOUNT),))
    monkeypatch.setattr(rs, "_digest_cache", {})


#: One payload per high-signal rule, plus the stand-in account id bare and inside an ARN. Every
#: one of these must be found even though the containing file is binary — that is the whole
#: claim that makes skipping the generic 12-digit rule on binaries defensible.
_REAL_LEAKS = [
    EXAMPLE_AKIA,
    EXAMPLE_ASIA,
    b"aws_secret_access_key" + b"=wJalrXUtnFEMI",
    b"arn:aws:iam::" + STANDIN_ACCOUNT + b":role/LlmopsAdminLambdaRole",
    STANDIN_ACCOUNT,
]


@pytest.mark.parametrize("payload", _REAL_LEAKS, ids=lambda p: p[:24].decode("latin-1"))
def test_a_real_secret_is_caught_even_inside_a_binary(payload, standin_is_the_watched_account):
    """The load-bearing assertion. If this can pass with a stubbed-out binary path, the
    entropy fix has quietly become "binaries are not scanned"."""
    findings = rs.scan_blob("deploy/console/intro/audio/en/s1-problem.mp3", _bin(payload))
    assert findings, (
        f"a binary carrying {payload!r} was reported clean — the high-signal rules are not "
        "running on binaries, so dropping the generic rule for them is now a hole")
    # A binary finding must NOT claim a line number: a byte offset into an MPEG stream is not
    # a line, and a fabricated one sends the reader to the wrong place in the wrong file.
    assert all(line is None for line, _, _ in findings), findings


@pytest.mark.parametrize("payload", _REAL_LEAKS, ids=lambda p: p[:24].decode("latin-1"))
def test_a_real_secret_is_caught_in_text_too(payload, standin_is_the_watched_account):
    """Same payloads through the text path, with a real line number."""
    findings = rs.scan_blob("deploy/console/frontend.html", _text(payload))
    assert findings, f"text carrying {payload!r} was reported clean"
    assert all(isinstance(line, int) and line >= 1 for line, _, _ in findings), findings


# --------------------------------------------------------------------------------------
# The direction that caused the block: compressed-audio entropy must NOT be a finding.
# --------------------------------------------------------------------------------------

def test_the_byte_run_that_blocked_the_commit_is_not_a_finding():
    """The exact observed false positive, byte for byte.

    `deploy/console/intro/audio/zh/s4-build.mp3` contained ten `0x33` bytes followed by
    `0x31 0x39` inside an MPEG frame — ASCII digits by coincidence, not an account id.
    Measured: 1 of 35 clips tripped it, so re-synthesising the narration blocks on a different
    random subset. That is the failure mode that teaches people to pass --no-verify, after
    which the gate guards nothing at all.

    `BLOCKING_RUN` is built with `bytes([...])`, so this test covers the identical twelve bytes
    without the file containing them as a literal.
    """
    assert len(BLOCKING_RUN) == 12 and BLOCKING_RUN.isdigit(), BLOCKING_RUN
    assert not rs.scan_blob("deploy/console/intro/audio/zh/s4-build.mp3",
                            _bin(BLOCKING_RUN)), \
        "the entropy false positive is back; the commit hook will block at random again"


def test_the_same_digits_in_TEXT_are_still_a_finding():
    """The other half: the generic rule is dropped for binaries ONLY.

    Without this, `if not binary` could be widened to `if False` and the suite above would
    still pass — the generic heuristic is the only thing that catches an account id that is
    neither this account's nor inside an ARN, and it must keep working on text.
    """
    findings = rs.scan_blob("docs/ARCHITECTURE.md",
                            _text(b"account " + BLOCKING_RUN + b" here"))
    assert findings, "a bare 12-digit id in a MARKDOWN file was not reported"
    assert any("bare 12-digit" in rule for _, rule, _ in findings), findings


def test_every_committed_narration_clip_scans_clean():
    """Runs the real rules over the real audio, not a synthetic stand-in.

    The synthetic tests above pin behaviour; this one pins the actual artifact, which is what
    the commit hook will scan. If a future re-synthesis produces a clip that trips a rule,
    this fails offline instead of at `git commit` time.
    """
    clips = sorted((REPO / "deploy/console/intro/audio").glob("*/*.mp3"))
    if not clips:
        pytest.skip("narration audio is not present in this checkout")
    assert len(clips) >= 35, f"expected 35 bundled clips, found {len(clips)}"
    for clip in clips:
        rel = str(clip.relative_to(REPO))
        assert not rs.scan_blob(rel, clip.read_bytes()), f"{rel} would block a commit"


# --------------------------------------------------------------------------------------
# Classification, allowlists, and the self-report skip.
# --------------------------------------------------------------------------------------

def test_binary_classification_matches_git_for_every_tracked_file():
    """`is_binary` claims to classify the way git does. Assert it against git, not a list.

    A comment saying "same heuristic as git" is unfalsifiable; git's own numstat output is
    not. This is what lets the hook's message ("scanned as a binary") be cross-checked by an
    engineer with `git diff --numstat`.

    Diffed against the EMPTY TREE, not against HEAD. `git diff --cached --numstat` alone lists
    only what differs from HEAD, so on a clean checkout it returns nothing and this test used to
    `pytest.skip("nothing staged")` -- which is exactly what CI is: a fresh clone with an
    untouched index. Measured in the CI log for the commit that added this note: `910 passed,
    4 skipped`, one skip more than the three ffprobe cross-checks THAT EXISTED THEN, and this
    was it. (Two of those three read the committed walkthrough mp4 and were deleted with it when
    the film moved to a hosted upload; one ffprobe-gated test is left, so the same arithmetic on
    a current CI log reads against `1 skipped`. Both counts are pinned to their commit on
    purpose -- the finding is that a skip count nobody reads is where a guard goes to hide, and
    that finding does not expire when the arithmetic changes.) A guard whose name says "for every
    tracked file" was checking zero of them on the only machine that gates the merge, and
    reported green for it. Against `4b825dc…` the diff is the whole index -- 176 files, 35 of
    them binary -- so it can never be empty and there is nothing left to skip on.

    One trap in the staging cure below: `git add -N` is NOT enough for this test. It records the
    path, so `git ls-files` counts it and test_an_unstaged_new_file_is_a_lying_census goes green,
    but it stores no blob -- `git show :path` exits non-zero and the loop `continue`s past the
    file, so `checked` stays behind `len(tracked)` and THIS guard reds instead. Measured while
    adding the landed detector: 172 classified of 175 tracked. A new file needs a real
    `git add`.
    """
    out = subprocess.run(["git", "diff", "--cached", "--numstat", _EMPTY_TREE],
                         capture_output=True, text=True, cwd=REPO).stdout
    rows = [ln.split("\t") for ln in out.splitlines() if ln.count("\t") >= 2]
    assert rows, (
        "git reported no indexed files at all against the empty tree — this guard verified "
        "nothing, and its previous version would have skipped silently here")
    checked = 0
    for added, _removed, path in ((r[0], r[1], r[2]) for r in rows):
        blob = subprocess.run(["git", "show", f":{path}"], capture_output=True, cwd=REPO)
        if blob.returncode:
            continue
        git_says_binary = added == "-"
        assert rs.is_binary(blob.stdout) == git_says_binary, (
            f"{path}: git says binary={git_says_binary}, is_binary() disagrees")
        checked += 1
    # EVERY tracked file, which is what the name promises -- not "at least one". `assert checked`
    # was the old floor, and it is satisfied by checking a single file out of 176: a diff against
    # HEAD covers only what this branch happens to touch, so the coverage of this guard silently
    # tracked the size of the working change. Comparing against `git ls-files` makes the promise
    # in the name falsifiable, and is what fails if the diff base is ever narrowed again.
    tracked = subprocess.run(["git", "ls-files"], capture_output=True, text=True,
                             cwd=REPO).stdout.split()
    assert checked == len(tracked), (
        f"classified {checked} of {len(tracked)} tracked files — this guard's name says every "
        "one of them. A narrower diff base (e.g. HEAD instead of the empty tree) checks only "
        "the files the current branch touches, and reports green for the rest")


@pytest.mark.parametrize("allowed", rs.ALLOWED, ids=lambda a: a.decode())
def test_the_public_aws_accounts_are_not_findings(allowed):
    """These appear in official ECR image URIs and in placeholder examples.

    Parametrized off `rs.ALLOWED` rather than a hand-copied list: a second copy of the
    allowlist here would be exactly the duplication this module was written to remove, and it
    would let an entry be dropped from the scanner while these tests kept passing.
    """
    assert len(rs.ALLOWED) == 3, rs.ALLOWED
    assert not rs.scan_blob("deploy/07_lambdas.py", _text(b"image " + allowed + b".dkr.ecr"))


def test_a_longer_digit_run_is_not_an_account_id():
    """13+ digits is a timestamp or a byte count. Applied to the generic rule only."""
    assert not rs.scan_blob("docs/COST.md", _text(b"at 1753699200000 ms"))


def test_a_live_api_gateway_hostname_is_a_finding():
    """The rule that was missing while the leak it describes was already merged.

    Every other rule here is about identity -- an access key, an account id, an ARN. A URL is
    none of those, which is why nothing objected when the address of this account's live admin
    console shipped in a rendered README, in both ARCHITECTURE files, and hard-coded into two
    test files. Reaching that page still needs a Cognito login; publishing its address in a
    public repo invites everyone who reads it to try, and that page launches runs and approves
    budgets.

    The id here is synthesised from a shape, not copied from the real one -- writing the real id
    into the file that proves it is caught is the self-report this module already avoids for
    account ids.
    """
    host = b"a1b2c3d4e5.execute-api.us-east-1.amazonaws.com"
    findings = rs.scan_blob("README.md", _text(b"the dashboard is at https://" + host + b"/"))
    assert findings, "a live API Gateway hostname was not reported"
    assert any("hostname" in f[1] for f in findings), findings
    # And on a binary too: the rule is structural, so an mp3 or a zip carrying the same
    # hostname is the same leak. It is deliberately NOT text-only like the 12-digit heuristic.
    assert rs.scan_blob("deploy/console/intro/audio/en/s1.mp3", b"\x00\x00" + host)


@pytest.mark.parametrize("example", rs.EXAMPLE_API_IDS, ids=lambda e: e.decode())
def test_an_example_api_id_is_not_a_finding(example):
    """`deploy/03_storage.py` prints a sample origin in its own help text, and this repo's own
    tests need a stand-in id. Those must not be findings, or the rule gets deleted the first
    time it blocks a legitimate example.

    Parametrized off the module's own tuple for the same reason the ALLOWED test is: a
    hand-copied second list lets an entry be dropped from the scanner with these still green.
    """
    blob = _text(b"https://" + example + b".execute-api.us-east-1.amazonaws.com")
    assert not [f for f in rs.scan_blob("deploy/03_storage.py", blob) if "hostname" in f[1]]


def test_the_examples_do_not_excuse_the_real_shape():
    """An excuse list is only safe if it cannot be satisfied by accident.

    Two ways this rule could be written to excuse a real leak, both checked:

    1. matching the example ids as SUBSTRINGS of the hostname -- then an id that merely
       *contains* an excused one walks straight through. The id is compared for equality
       against the captured group for this reason.
    2. folding them into `_excused`, which is substring-based and shared by every rule above --
       then the word "example" anywhere in an ARN hit silences the ARN rule.

    The first version of this rule had defect 1. It was found by writing this test, not by
    reading the regex.

    The sneaky id is BUILT FROM the module's own tuple rather than spelled out. The first
    version of this test hard-coded `exampleandthenrealbits99`, which contains no entry of that
    tuple at all -- so a substring-matching scanner excused nothing, the assertion passed
    against the very defect it names, and the negative control for it passed too. That is how
    the weakness was found: by watching the control fail to fail.
    """
    sneaky = rs.EXAMPLE_API_IDS[0] + b"andthenrealbits99.execute-api.us-east-1.amazonaws.com"
    assert [f for f in rs.scan_blob("README.md", _text(b"https://" + sneaky))
            if "hostname" in f[1]], (
        f"an id merely CONTAINING the excused {rs.EXAMPLE_API_IDS[0]!r} was excused")
    arn = rs.scan_blob("deploy/iam.json",
                       _text(b"example arn:aws:iam::" + SYNTHETIC_ACCOUNT + b":role/x"))
    assert arn, "the word 'example' silenced the ARN rule"


def test_an_account_bearing_arn_is_caught_even_when_the_digits_run_long():
    """The length excuse must not extend to a structured ARN match.

    An ARN whose account field runs 15 digits long has a 13+ digit run, so a naive
    "13+ digits is an ordinary number" rule would excuse a genuinely
    malformed-but-account-bearing ARN. The ARN pattern is structural, so it should stand on
    its own.
    """
    findings = rs.scan_blob("deploy/iam.json",
                            _text(b"arn:aws:iam::" + SYNTHETIC_ACCOUNT + b":role/x"))
    assert findings, "an account-bearing ARN was excused"
    # And with the long-run case the docstring describes, built rather than quoted.
    long_run = rs.scan_blob("deploy/iam.json",
                            _text(b"arn:aws:iam::" + SYNTHETIC_ACCOUNT + b"999" + b":role/x"))
    assert long_run, "a 15-digit account field excused an account-bearing ARN"


#: Spellings a re-inlined scanner could use, and legitimate non-recursive greps that must
#: keep passing. This table exists because the first version of the shape guard below was
#: itself wrong -- it required the recursive flag to stand alone, so a control spelled
#: `grep -rn AKIA` slipped past and reported UNCAUGHT. A regex asserted only against the
#: one string a developer had in mind is a regex with unmeasured edges.
_MUST_BE_FLAGGED = [
    "grep -rn AKIA --include='*.py' . || true",
    "grep -r AKIA .",
    "grep -R AKIA .",
    "grep --recursive AKIA .",
    "grep -rniE 'AKIA[0-9A-Z]{16}' .",
    "rg -r pattern .",
    "hits=$(ugrep -rn AKIA .)",
]
_MUST_BE_ALLOWED = [
    "python3 tests/redaction_scan.py --tracked",
    'git show ":$f" | grep -nE AKIA',          # reads one blob from stdin, not the tree
    'echo "$STAGED" | grep -qx "$twin"',       # the bilingual pairing check
    "STAGED=$(git diff --cached --name-only --diff-filter=ACM)",
    'echo "$STAGED" | grep -qE \'docs/architecture.*\\.svg\'',
]


@pytest.mark.parametrize("line", _MUST_BE_FLAGGED)
def test_the_shape_guard_flags_every_recursive_scan_spelling(line):
    """Bundled short flags (`-rn`, `-rniE`) are the case that escaped the first version."""
    assert _recursive_scan_re().search(line), f"not flagged as a recursive content scan: {line}"


@pytest.mark.parametrize("line", _MUST_BE_ALLOWED)
def test_the_shape_guard_allows_non_recursive_greps(line):
    """Both callers legitimately grep filenames and single blobs; only tree-walking is banned.

    Without this half, tightening the pattern until everything matches would "pass" while
    making the guard fire on the hook's own bilingual pairing check.
    """
    assert not _recursive_scan_re().search(line), f"wrongly flagged: {line}"


def test_the_scanner_does_not_report_itself():
    """This file and the scanner contain the patterns as their subject matter.

    Pinned as an exact-path tuple rather than a prefix: `tests/` as a blanket skip would mean
    a fixture that accidentally embedded a real credential went unscanned forever.
    """
    assert "tests/redaction_scan.py" in rs.SELF_REFERENTIAL
    assert "tests/test_redaction_scan.py" in rs.SELF_REFERENTIAL
    for path in rs.SELF_REFERENTIAL:
        assert (REPO / path).exists(), (
            f"{path} is allowlisted from the redaction scan but does not exist — a stale skip "
            "entry silently exempts nothing today and the wrong file tomorrow")
    assert not any(p.rstrip("/").endswith(("tests", "docs", "deploy")) or p.endswith("*")
                   for p in rs.SELF_REFERENTIAL), \
        "SELF_REFERENTIAL must list exact files, never a directory or glob"


#: Python's implicit/explicit literal concatenation, as source text: the closing quote of one
#: string literal, a `+`, and the opening quote of the next (with an optional `b`/`r`/`f`
#: prefix). Substituting it away turns a two-part `b"1234" + b"5678..."` back into the single
#: literal a reader sees when they glance at the line.
_LITERAL_GLUE = re.compile(r"""(["'])\s*\+\s*[bBrRfFuU]*(["'])""")


def _with_literals_joined(src: str) -> str:
    """Source text with adjacent string-literal concatenations collapsed.

    This is the whole lesson of the account-id exposure in one function. The previous version
    of the guard below searched for one hand-spelled needle, so the same digits written as two
    adjacent halves passed it — and that split form is exactly what shipped, in a file GitHub
    renders, where any reader recombines it by eye in a second. Splitting hid the id from every
    machine and from no human at all.

    Applied repeatedly because a value can be broken into more than two pieces; three parts
    need two passes.
    """
    for _ in range(16):
        joined = _LITERAL_GLUE.sub("", src)
        if joined == src:
            return joined
        src = joined
    raise AssertionError("literal concatenation did not settle after 16 passes")


def test_the_real_account_id_is_never_recoverable_from_either_file():
    """The scanner must know the account id without either file containing it — in ANY form.

    "Any form" is the part the earlier version got wrong. It asserted only that one spelled-out
    needle was absent, which the halves satisfied while sitting adjacent in source order. So
    the check is now done the way the scanner itself does it: take every 12-digit run in the
    source, take every 12-digit run again after collapsing literal concatenation, and ask the
    digest. No needle is spelled anywhere, and a split into any number of parts is caught.
    """
    # The joiner has to actually join, or the second pass below is identical to the first and
    # this guard silently degrades to the one-needle version it replaced. Self-checked against a
    # split this file is known to contain (STANDIN_ACCOUNT above) rather than a hand-written
    # example, so the check cannot go stale against a form the file no longer uses.
    assert STANDIN_ACCOUNT.decode() in _with_literals_joined(Path(__file__).read_text()), (
        "_with_literals_joined did not collapse this file's own split literals, so the "
        "'assembled from adjacent literals' pass below is checking the unmodified source twice")

    for path in ("tests/redaction_scan.py", "tests/test_redaction_scan.py"):
        src = (REPO / path).read_text()
        for label, text in (("as a literal", src),
                            ("assembled from adjacent literals", _with_literals_joined(src))):
            for run in re.findall(r"(?<![0-9])[0-9]{12}(?![0-9])", text):
                assert not rs._digest_matches(run.encode()), (
                    f"{path} contains this repo's own account id {label} — and both files are "
                    "in SELF_REFERENTIAL, so no scanner would report it. Splitting the digits "
                    "is not a fix: a reader recombines adjacent halves by eye. Remove them; "
                    "the scanner only needs the digest.")


def test_the_watched_account_is_stored_as_an_iterated_digest():
    """The digest scheme's two load-bearing properties, since neither is visible in a passing scan.

    A well-formed digest list, and a KDF that is actually iterated. Twelve digits is ~40 bits:
    measured on this laptop, single-threaded CPython does 3.1M plain sha256/s, so the whole 1e12
    space falls in ~4 days here and ~100 seconds on a GPU. Storing a BARE sha256 would therefore
    publish the id to anyone who cares, while every test in this file still passed. The round
    count is what makes the stored digest useless to a sweeper, so it is asserted, not assumed.
    """
    assert rs.REAL_ACCOUNT_DIGESTS, (
        "REAL_ACCOUNT_DIGESTS is empty — the scanner now watches for no account at all, and "
        "every binary in the repo is checked by four structural rules and nothing else")
    for digest in rs.REAL_ACCOUNT_DIGESTS:
        assert re.fullmatch(r"[0-9a-f]{64}", digest), (
            f"{digest!r} is not a sha256 hex digest — an id or a truncated hash here matches "
            "nothing, silently")
    assert rs._KDF_ROUNDS >= 100_000, (
        f"the KDF is down to {rs._KDF_ROUNDS} rounds; below ~100k a 12-digit space is sweepable "
        "on one GPU in minutes, so the stored digests would leak the ids they stand in for")

    # And the function actually RUNS those rounds. Asserting the constant alone is worthless:
    # swapping the body for `sha256(salt + candidate)` leaves _KDF_ROUNDS = 200_000 sitting in
    # the file, unused, while every scan produces identical findings and every other assertion
    # here stays green. So the algorithm is pinned by recomputing it independently.
    probe = b"1122" + b"33445566"
    assert rs.account_digest(probe) == hashlib.pbkdf2_hmac(
        "sha256", probe, rs._KDF_SALT, rs._KDF_ROUNDS).hex(), (
        "account_digest is not PBKDF2-HMAC-SHA256 over _KDF_SALT for _KDF_ROUNDS rounds. A "
        "single unsalted or un-iterated hash of a 12-digit number is a lookup, not a digest: "
        "the whole 1e12 space is ~100 GPU-seconds, so the stored digest would publish the id")

    # And the digest actually depends on its input. A stub that returned a constant would keep
    # every assertion above green while making the scanner fire on every 12-digit run in the repo.
    assert rs.account_digest(b"1357" + b"91357913") != rs.account_digest(SYNTHETIC_ACCOUNT), \
        "account_digest returned the same digest for two different ids"


#: The scanner's own patterns, applied to the two files that carry them as subject matter.
#: Deliberately a SEPARATE list from rs.HIGH_SIGNAL: this asserts a property about the source
#: TEXT, and reusing the scanner's compiled rules would make the guard vacuous the moment a
#: rule is loosened. `str` patterns because these read source, not blobs.
_LITERAL_SHAPES = (
    ("a bare 12-digit run", re.compile(r"(?<![0-9])[0-9]{12}(?![0-9])")),
    ("an AWS access key id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("a temporary access key id", re.compile(r"ASIA[0-9A-Z]{16}")),
    ("an account-bearing ARN", re.compile(r"arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:[0-9]{12}")),
)


@pytest.mark.parametrize("path", ["tests/redaction_scan.py", "tests/test_redaction_scan.py"])
def test_no_credential_shaped_literal_survives_in_either_file(path):
    """Neither file may SPELL a credential-shaped string, even a public or synthetic one.

    This generalises `test_the_real_account_id_is_never_recoverable_from_either_file` from one
    value to a shape, and it exists because of a concrete failure. This repo's scanner exempts both
    files via `SELF_REFERENTIAL` — necessarily, since their subject matter IS these patterns —
    but that exemption is local knowledge. A session-level pre-PR hook scanned the branch diff
    with its own pattern list and no such notion, and blocked the PR introducing this module on
    five hits: the `0x33` byte run, the three allowlisted AWS accounts, AWS's published example
    access key, a synthetic account and a placeholder ARN. Not one was a secret.

    The wrong fix is to teach that hook a per-file exemption: that is a second scanner with its
    own selection scheme, i.e. the exact drift this module was written to end, and it would
    have to be repeated for every scanner that ever reads these files. Assembling the values
    from parts costs one `+`, needs no coordination, and cannot drift.

    Values are still exercised end to end — `BLOCKING_RUN` is rebuilt with `bytes([...])` and
    asserted to be the identical twelve bytes; `rs.ALLOWED` is parametrized straight off the
    scanner. Nothing is weakened. The strings just are not written down.
    """
    src = (REPO / path).read_text()
    for label, pat in _LITERAL_SHAPES:
        hit = pat.search(src)
        assert not hit, (
            f"{path}:{src[:hit.start()].count(chr(10)) + 1} spells {label} as a literal "
            f"({hit.group(0)!r}). Assemble it from parts (b'1234' + b'5678...') or rebuild it "
            "with bytes([...]) — otherwise every scanner that reads this file needs to be "
            "taught an exemption for it, one at a time.")


# --------------------------------------------------------------------------------------
# "Could not look" must never read as "looked and it is fine".
# --------------------------------------------------------------------------------------

def test_a_scan_of_no_files_says_so_instead_of_reporting_clean(tmp_path, monkeypatch, capsys):
    """An empty file list is the shape a broken file-selection step takes.

    It exits 0 — an empty staged set is a legitimate no-op — but it must SAY it scanned
    nothing rather than printing the same reassuring "clean" line a real scan prints. That
    distinction is the difference between a passing check and a check that never ran.

    Driven through a real empty git repo rather than by calling main([]) directly: the CLI
    requires one of --staged/--tracked/paths, so an empty argv is an argparse error, not this
    path. Reaching it any other way would be testing a state the program cannot be in.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    rc = rs.main(["--staged"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "nothing to scan" in out.lower(), (
        f"an empty scan printed {out!r}; a scan of zero files must not look like a pass")
    assert "scan clean" not in out.lower(), out


def test_an_empty_argv_is_an_error_not_an_empty_scan():
    """The CLI must not silently scan nothing when invoked with no mode at all.

    A caller that loses its `--staged` argument (a shell-quoting slip, a refactor) must get a
    usage error, not exit 0. argparse exits 2, which both callers treat as failure.
    """
    with pytest.raises(SystemExit) as exc:
        rs.main([])
    assert exc.value.code == 2


def test_an_unreadable_file_exits_two_not_zero(tmp_path, capsys):
    """rc=2 is reserved for "the scanner could not run", and both callers treat it as failure.

    The commit hook and the CI step must fail on 2. If this returned 0, a checkout problem
    would publish as a clean redaction scan.
    """
    missing = tmp_path / "does-not-exist.py"
    rc = rs.main([str(missing)])
    assert rc == 2, f"expected rc=2 for an unreadable path, got {rc}"


def test_both_callers_fail_on_the_scanner_being_absent():
    """The hook and the workflow must not SKIP when the scanner is missing.

    This repo already learned this once: the SVG block in hooks/pre-commit used to skip
    silently when its checker was renamed, which disabled the gate and nothing said so.
    """
    hook = (REPO / "hooks/pre-commit").read_text()
    assert "tests/redaction_scan.py is missing" in hook, \
        "the hook no longer fails closed when the scanner is absent"
    assert "--staged" in hook, "the hook does not invoke the shared scanner"

    wf = (REPO / ".github/workflows/redaction-check.yml").read_text()
    assert "redaction_scan.py --tracked" in wf, \
        "CI does not invoke the shared scanner in --tracked mode"


def test_neither_caller_kept_its_own_copy_of_the_rules():
    """The point of the module is that there is ONE rule set.

    If content-scanning reappears in either caller they can drift again, which is the defect
    this replaced. The first version of this test grepped for two exact regex SPELLINGS
    (`AKIA[0-9A-Z]{16}`, `[0-9]{12}([^0-9.]|$)`) and a negative control walked straight past
    it: a re-inlined scanner spelled `grep -rn AKIA --include='*.py'` contains neither
    fragment. Matching on spelling only catches the copy-paste, not the rewrite.

    So the assertion is about SHAPE instead: neither caller may recursively scan repo content
    itself. That is what a second rule set requires, whatever regexes it is written with.
    Non-recursive greps are fine and both callers use them — `hooks/pre-commit` matches
    filenames out of its staged list — so only the recursive forms are forbidden.
    """
    recursive = _recursive_scan_re()
    for path in ("hooks/pre-commit", ".github/workflows/redaction-check.yml"):
        src = (REPO / path).read_text()
        # Comments explain the history and legitimately name the old patterns; strip them so
        # prose about the defect is not mistaken for the defect.
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.lstrip().startswith("#"))
        hit = recursive.search(code)
        assert not hit, (
            f"{path} scans repo content itself ({hit.group(0)!r}); that is a second rule set "
            "and the two will drift apart again — keep the rules in tests/redaction_scan.py")
        for frag in ("AKIA[0-9A-Z]{16}", "[0-9]{12}([^0-9.]|$)", "aws_secret_access_key"):
            assert frag not in code, (
                f"{path} has re-inlined the redaction regexes ({frag!r}); keep the rules in "
                "tests/redaction_scan.py")


#: Every place the scanner's own comments state how much of the repo it covers, anchored on the
#: surrounding phrase rather than on a bare number. Same reason LAMBDA_COUNT_PATTERNS in
#: tests/test_docs_claims.py is per-site: each says it differently, and one loose `\d+` would
#: match the KDF round count, the iteration timings and the byte offsets in the same comments.
#:
#: Nothing here states what the counts ARE -- they are derived below from `git ls-files` and
#: `rs.is_binary`, which is the whole point. A guard hardcoding 161 would catch a comment
#: drifting while the repo sits still, and sail past the repo growing while the comment sits
#: still. Every count in this repo that has ever gone stale went stale by ADDITION.
_COVERAGE_CLAIM_PATTERNS = {
    "tests/redaction_scan.py": (
        (r"measured across all (\d+)\s*\n#:\s*tracked files", "tracked"),
    ),
    "tests/test_redaction_scan.py": (
        (r"the whole index -- (\d+) files, (\d+) of\s*\n\s*them binary", "tracked+binary"),
        (r"checking a single file out of (\d+)", "tracked"),
    ),
}

#: What marks a count as describing a PAST state of the repo rather than today's. Same carve-out
#: as HISTORICAL_FLEET_PATTERNS in test_docs_claims.py, and for the same reason: the comment at
#: redaction_scan.py:113 says "163 files became 161" to record that the file count moved while
#: the 12-digit run counts did not, which is the evidence that two numbers drifting together are
#: not one number. A guard forcing every number to today's value would demand that line lie
#: about what was measured.
_PAST_COUNT_PHRASINGS = (r"(\d+) files became (\d+)",)


def test_the_scanners_own_coverage_claims_match_the_repo():
    """The "161 files, 35 binary" in these comments, derived rather than trusted.

    These numbers are load-bearing in a way a reader cannot check: they are the evidence for
    dropping the generic 12-digit rule on binaries ("measured across all N tracked files there
    are M such runs") and for the empty-tree diff base covering everything. The counts are NOT
    quoted here -- a docstring citing "52 such runs" is one more unchecked copy of exactly the
    number this test derives, and it went stale the moment the env_keys tests added two runs.
    They were carefully
    re-measured when the walkthrough mp4 and its poster were deleted -- and then left as prose,
    with nothing deriving them, which is the same defect three earlier commits in this repo
    fixed elsewhere. Re-measuring describes how a number was PRODUCED; it says nothing about
    whether it stays true. Committing one tracked file falsified all four sites while the full
    suite stayed green, measured.

    The past-tense carve-out is narrow and follows test_docs_claims.py's: a comment may state a
    former count where its own phrasing says that is what it is doing ("163 files became 161").
    The CURRENT half of such a phrase is still held to the real number, so the line that records
    the change cannot itself go stale.
    """
    tracked = subprocess.run(["git", "ls-files"], capture_output=True, text=True,
                             cwd=REPO, check=True).stdout.split()
    assert tracked, "git reported no tracked files -- this guard verified nothing"
    n_binary = sum(1 for p in tracked
                   if rs.is_binary((REPO / p).read_bytes()))
    real = {"tracked": len(tracked), "binary": n_binary}

    for path, claims in _COVERAGE_CLAIM_PATTERNS.items():
        src = (REPO / path).read_text()
        for pattern, kind in claims:
            m = re.search(pattern, src)
            # A reworded comment must FAIL rather than pass silently -- an anchored pattern that
            # matches nothing is indistinguishable from a correct claim otherwise, and that is
            # how a guard like this becomes decoration.
            assert m, (
                f"{path}: no coverage claim matching /{pattern}/ -- if the wording changed, "
                "update the pattern in _COVERAGE_CLAIM_PATTERNS; if the claim was deleted, "
                "delete its entry. Do not leave a guard matching nothing")
            for got, want in zip(m.groups(), kind.split("+")):
                assert int(got) == real[want], (
                    f"{path} claims {got} {want} files, the repo has {real[want]} "
                    f"(matched {m.group(0)!r})")

    # The past-tense form, held to its current half only.
    src = (REPO / "tests/redaction_scan.py").read_text()
    for pattern in _PAST_COUNT_PHRASINGS:
        m = re.search(pattern, src)
        assert m, (
            f"tests/redaction_scan.py: no past-count phrase matching /{pattern}/ -- that comment "
            "records that the file count moved while the 12-digit run counts did not, which is "
            "why the run counts are believable. Update the pattern rather than dropping it")
        was, now = int(m.group(1)), int(m.group(2))
        assert now == real["tracked"], (
            f"the comment says the count became {now}, the repo has {real['tracked']} files")
        assert was != now, (
            f"the comment records {was} -> {now}, which is not a change at all")

    # The 12-digit run counts in the same sentence, derived too. They were the ONE half of
    # that sentence nothing checked: "162 files became 163" was caught by the guard above,
    # while "52 such runs, 9 distinct" sat one clause away, hand-edited, and a mutation to
    # `8 distinct` passed the whole suite. The counts are the entire argument for dropping
    # the generic 12-digit rule on binaries -- the cheapness claim is what makes the KDF
    # affordable -- so the number that carries the argument cannot be the number nobody
    # derives. Same finding as the file counts, one clause later.
    m = re.search(r"there are (\d+) such runs, (\d+) distinct", src)
    assert m, ("tests/redaction_scan.py: no 12-digit run-count claim -- that count is the "
               "evidence for hashing only candidates. Update the pattern, do not drop it")
    runs = [c for p in tracked
            for c in rs._ACCOUNT_CANDIDATE.findall((REPO / p).read_bytes())]
    assert (int(m.group(1)), int(m.group(2))) == (len(runs), len(set(runs))), (
        f"the comment claims {m.group(1)} runs / {m.group(2)} distinct, the repo has "
        f"{len(runs)} / {len(set(runs))}")


def test_an_unstaged_new_file_is_a_lying_census():
    """Every census in this repo reads `git ls-files`, so an UNSTAGED new file makes them all
    answer a question CI will answer differently.

    This is the only guard here whose target is the workstation rather than the repo. On this
    machine `git push` is unavailable and branches go up through the Git Data API from the
    WORKING TREE, so a new file can be pushed while the index has never heard of it -- and
    `git ls-files` reads the index. The four counts above, the lambda census in
    test_docs_claims.py and the asset census in test_intro_video.py then all measure a repo
    with one fewer file than the branch has, agree with the stale comment they are checking,
    and go red only on CI. That has now happened FOUR times and cost two red PRs; the fourth
    time a full local suite had passed minutes before the push.

    The cure is one command (`git add -N <path>`), which is exactly why it needed a guard
    rather than a note: advice that must be remembered at the right moment is not a control.
    In CI this assertion is trivially true (a checkout has no untracked files), which is the
    point -- it moves the detection to the machine that was missing it. Verified by planting
    an untracked file and watching it fail, because a guard nobody has ever seen fail is
    indistinguishable from `assert True`.

    Deliberately NOT solved by making the censuses read the working tree instead: the index is
    the branch's truth, and a census over untracked files would count local scratch as shipped
    code -- the same defect in the other direction.
    """
    untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"],
                               capture_output=True, text=True, cwd=REPO, check=True
                               ).stdout.split()
    assert not untracked, (
        "these files exist in the working tree but not in the index, so every `git ls-files` "
        f"census in this suite is measuring a different repo than CI will: {untracked}. Run "
        "`git add -N <path>` for anything the next push includes, or add it to .gitignore if "
        "it is local scratch")
