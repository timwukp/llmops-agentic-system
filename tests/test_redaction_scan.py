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

#: One payload per high-signal rule, plus this repo's own account id bare. Every one of these
#: must be found even though the containing file is binary — that is the whole claim that makes
#: skipping the generic 12-digit rule on binaries defensible.
_REAL_LEAKS = [
    EXAMPLE_AKIA,
    EXAMPLE_ASIA,
    b"aws_secret_access_key" + b"=wJalrXUtnFEMI",
    b"arn:aws:iam::" + b"677207" + b"132843" + b":role/LlmopsAdminLambdaRole",
    b"677207" + b"132843",
]


@pytest.mark.parametrize("payload", _REAL_LEAKS, ids=lambda p: p[:24].decode("latin-1"))
def test_a_real_secret_is_caught_even_inside_a_binary(payload):
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
def test_a_real_secret_is_caught_in_text_too(payload):
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
    """
    out = subprocess.run(["git", "diff", "--cached", "--numstat"],
                         capture_output=True, text=True, cwd=REPO).stdout
    rows = [ln.split("\t") for ln in out.splitlines() if ln.count("\t") >= 2]
    if not rows:
        pytest.skip("nothing staged to compare against")
    checked = 0
    for added, _removed, path in ((r[0], r[1], r[2]) for r in rows):
        blob = subprocess.run(["git", "show", f":{path}"], capture_output=True, cwd=REPO)
        if blob.returncode:
            continue
        git_says_binary = added == "-"
        assert rs.is_binary(blob.stdout) == git_says_binary, (
            f"{path}: git says binary={git_says_binary}, is_binary() disagrees")
        checked += 1
    assert checked, "no staged blobs could be read — this guard verified nothing"


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


def test_the_real_account_id_is_never_a_literal_in_this_repo():
    """The scanner must know the account id without the repo containing it.

    Both this file and the scanner assemble it from halves. If someone "simplifies" either to
    a literal, the repo now contains the exact string it exists to keep out — and because both
    files are in SELF_REFERENTIAL, no scanner would report it.
    """
    # The needle is assembled here too. Spelling it out inside this very assertion is what
    # the first version of this test did, and it failed on itself — correctly: the string
    # would then be in the repo, in a file that no scanner inspects.
    needle = "677207" + "132843"
    for path in ("tests/redaction_scan.py", "tests/test_redaction_scan.py"):
        src = (REPO / path).read_text()
        assert needle not in src, (
            f"{path} contains the real account id as a literal; keep it assembled from parts")
    # And the assembled value is what we think it is: 12 digits, and the one the scanner uses.
    assert rs.REAL_ACCOUNT_IDS == (b"677207" + b"132843",)
    for acct in rs.REAL_ACCOUNT_IDS:
        assert re.fullmatch(rb"[0-9]{12}", acct), acct


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

    This generalises `test_the_real_account_id_is_never_a_literal_in_this_repo` from one value
    to a shape, and it exists because of a concrete failure. This repo's scanner exempts both
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
