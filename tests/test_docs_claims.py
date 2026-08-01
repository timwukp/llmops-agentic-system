"""Keep the numbers the docs assert in step with the numbers the repo produces.

docs/TEST_RESULTS.md is evidence: it prints a pass count next to the exact command
that produced it, so a reader can re-run it and check. That only works while the
number is true. It has now gone stale twice -- 274 was correct when written and became
a false claim as tests were added -- and nothing failed to say so, because a number in
a markdown table is invisible to the suite it describes.

The count comes from pytest's own collector, run as a subprocess with
``--collect-only``. The first draft of this file counted ``def test_*`` from the AST
instead, and was wrong by 11: three files use ``@pytest.mark.parametrize``, so one
function is several tests. Reimplementing collection means maintaining a second,
subtly different definition of "a test" -- and a guard that is wrong for its own
reasons is worse than no guard, because it fails without telling you which side drifted.
A subprocess avoids the recursion of a suite invoking itself.

The bilingual pair matters as much as the number: docs/TEST_RESULTS.md and its .zh-TW
counterpart are the same evidence for two audiences, and a count updated in one
language only is worse than one stale in both -- it reads as verified in whichever the
reader happens to open.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
DOCS = (REPO / "docs" / "TEST_RESULTS.md", REPO / "docs" / "TEST_RESULTS.zh-TW.md")

#: Docs that state how many skill sources exist, and of which kind.
DOC_SKILL_CLAIMS = (REPO / "AGENTS.md", REPO / "SECURITY.md", REPO / "PROJECT_STATE.md",
                    REPO / "README.md", REPO / "README.zh-TW.md",
                    REPO / "docs" / "ARCHITECTURE.md",
                    REPO / "docs" / "ARCHITECTURE.zh-TW.md",
                    REPO / "agents" / "README.md")

#: Counts in the docs are written as **N passed** / **N/N passed** / **N/N 通過**.
_CLAIM = re.compile(r"\*\*(\d+)(?:/(\d+))?\s*(?:passed|通過)\*\*")


def _collected_test_count() -> int:
    """Ask pytest how many tests it collects, so there is one definition of "a test"."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(REPO / "tests"), "--collect-only", "-q",
         "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=REPO, timeout=300)
    m = re.search(r"^(\d+) tests? collected", proc.stdout, re.MULTILINE)
    assert m, ("could not read a collection count out of pytest --collect-only; "
               f"rc={proc.returncode}\nstdout tail:\n{proc.stdout[-2000:]}"
               f"\nstderr tail:\n{proc.stderr[-1000:]}")
    return int(m.group(1))


def test_documented_test_counts_match_the_real_suite():
    """Every **N passed** claim in TEST_RESULTS must equal what pytest collects.

    Note the count includes the tests in this file, which is correct: the docs quote a
    total for ``pytest tests/ -q``, and these run under exactly that command.
    """
    expected = _collected_test_count()
    wrong = []
    for doc in DOCS:
        assert doc.exists(), f"{doc} is referenced as evidence but missing"
        for m in _CLAIM.finditer(doc.read_text()):
            claimed = int(m.group(1))
            if m.group(2) is not None and int(m.group(2)) != claimed:
                wrong.append(f"{doc.name}: {m.group(0)!r} is not of the form N/N")
            if claimed != expected:
                wrong.append(f"{doc.name}: claims {claimed}, pytest collects {expected}")
    assert not wrong, (
        "documented test counts have drifted from the suite: " + "; ".join(wrong)
        + f". Re-run the documented command and update all {len(DOCS)} language "
        "variants in the same commit.")


def test_both_language_variants_make_the_same_count_claims():
    """A count fixed in one language only reads as verified in whichever is opened."""
    per_doc = {doc.name: sorted(int(m.group(1)) for m in _CLAIM.finditer(doc.read_text()))
               for doc in DOCS}
    first, second = per_doc.values()
    assert first == second, (
        f"the bilingual evidence pair disagrees about its own numbers: {per_doc}. "
        "Same evidence, two audiences -- update them together.")


def _skill_sources() -> dict[str, dict[str, int]]:
    """Count skill sources per harness config, by source kind.

    Reads the configs rather than any prose, because the prose is what drifted: seven
    docs described `agents/*/harness.prod.json` and `deploy/05_mirror_skills.py` as
    shipped, and neither file has ever existed in any branch. A design read as a
    delivered feature is worse than an omission -- it stops anyone from building it.
    """
    counts = {}
    for cfg in sorted((REPO / "agents").glob("*/harness*.json")):
        kinds = {}
        for skill in json.loads(cfg.read_text()).get("skills") or []:
            for kind in ("git", "s3", "path", "awsSkills"):
                if kind in skill:
                    kinds[kind] = kinds.get(kind, 0) + 1
        counts[str(cfg.relative_to(REPO))] = kinds
    return counts


def test_the_skill_source_claims_match_the_harness_configs():
    """The docs state 19 git sources and 0 s3. Derive both from the configs.

    Stating "19 git, 0 s3" in prose is exactly the kind of claim that was false here
    before, so it is checked against the files. When the s3 migration lands, this test
    fails and names the new numbers -- which is the point: the docs must move with it,
    in the same commit, or the next reader is told the migration never happened.
    """
    counts = _skill_sources()
    git_n = sum(k.get("git", 0) for k in counts.values())
    s3_n = sum(k.get("s3", 0) for k in counts.values())
    claims = []
    for doc in DOC_SKILL_CLAIMS:
        text = doc.read_text()
        for m in re.finditer(r"(\d+)\s+(?:skill sources|sources)", text):
            claims.append((doc.name, int(m.group(1))))
        for m in re.finditer(r"(\d+)\s*個(?:技能)?來源", text):
            claims.append((doc.name, int(m.group(1))))
    # Per-doc, not "at least one doc". Requiring only one leaves the guard satisfied
    # while the count is quietly deleted from the other seven -- the check would then be
    # anchored to whichever file still happens to mention it. Each doc in the list either
    # states the count or is not in the list.
    stating = {name for name, _ in claims}
    silent = [d.name for d in DOC_SKILL_CLAIMS if d.name not in stating]
    assert not silent, (
        f"these docs no longer state a skill-source count: {silent}. The counts were "
        "wrong before, so dropping them removes the check rather than passing it. Either "
        "state the count or remove the file from DOC_SKILL_CLAIMS deliberately.")
    wrong = [f"{name} says {n}, configs have {git_n} git sources"
             for name, n in claims if n != git_n]
    assert not wrong, "; ".join(wrong) + f" (per-config: {counts})"
    assert s3_n == 0 or git_n == 0, (
        f"skill sources are now MIXED ({git_n} git, {s3_n} s3): {counts}. A partial "
        "migration means some harnesses read a pinned snapshot and others float on the "
        "skill repo's default branch, which is the drift the migration exists to stop. "
        "Update the docs and this guard together.")


def test_no_doc_claims_a_file_that_does_not_exist():
    """Seven docs pointed at two files that were never committed on any branch.

    `deploy/05_mirror_skills.py` and `agents/*/harness.prod.json` were cited as the
    production skill-mirror mechanism. Both absent -- so "production uses an S3 mirror"
    read as a shipped property of the system while all 19 sources were git.
    """
    cited = {}
    pattern = re.compile(r"`((?:deploy|agents|tests|tools|pipeline)/[A-Za-z0-9_./*-]+)`")
    # A path may be named precisely BECAUSE it does not exist -- the corrected prose says
    # so explicitly. Flagging those would force the docs to stop naming the gap, which is
    # the opposite of the fix. So a citation is exempt when its own line marks it absent,
    # and only there: the marker has to be next to the path, not elsewhere in the file.
    absent_markers = ("not exist", "never existed", "unbuilt", "not built", "no such file",
                      "has yet", "尚未", "從未存在", "不存在")
    # Paths belonging to OTHER repos are cited next to that repo's link, on the same line.
    external = re.compile(r"https?://github\.com/")
    for doc in sorted(REPO.glob("*.md")) + sorted((REPO / "docs").glob("*.md")) + [
            REPO / "deploy" / "README.md", REPO / "agents" / "README.md"]:
        if not doc.exists():
            continue
        # Scope is the PARAGRAPH, not the line: prose wraps, so "...`deploy/05_mirror_skills.py`\n
        # has never existed" puts the path and its disclaimer on different lines. A
        # line-scoped check re-flags exactly the corrected text it was meant to allow.
        for para in re.split(r"\n\s*\n", doc.read_text()):
            if any(mark in para for mark in absent_markers) or external.search(para):
                continue
            for m in pattern.finditer(para):
                path = m.group(1)
                missing = (not list(REPO.glob(path))) if "*" in path \
                    else (not (REPO / path).exists())
                if missing:
                    cited.setdefault(path, []).append(doc.name)
    assert not cited, (
        "docs cite repo paths that do not exist: "
        + "; ".join(f"{p} (in {', '.join(sorted(set(d)))})" for p, d in sorted(cited.items()))
        + ". Either build the file, or say plainly that it does not exist yet.")


# ── the counts that describe the spine, derived from the spine ─────────────────
# The "8 harness-task states on the happy path" claim in both ARCHITECTURE variants, and
# PROJECT_STATE's "9 states" and "Lambdas ×5", were all true when written and all became
# false by addition -- silently, in two languages, exactly like the test count above. Every
# one of them is derivable from the source it describes, so derive it.

def _happy_path_harness_states() -> int:
    """Harness-task states a successful default-mode run passes through.

    Walks the ASL from StartAt on the success edges rather than counting Task states,
    because `DataAudit` (audit mode only) and `RemediateFinetune` (gate-fail loop only) are
    real states that no happy-path run touches -- and the docs make the claim about the
    happy path specifically.

    "Happy path" is a two-part definition and the Choices need both halves: default
    pipeline mode (so PipelineModeChoice and RemediationChoice take their Default, skipping
    the audit branch and the remediation loop) AND the quality gate passing -- where the
    pass is the explicit `gate_passed: true` BRANCH and the Default is the failure edge.
    Taking Default everywhere walks the failure path out of QualityGateChoice and counts 5.
    """
    asl = json.loads((REPO / "orchestration/state_machine.asl.json").read_text())
    states, seen, count = asl["States"], set(), 0
    cur = asl["StartAt"]
    while cur and cur not in seen:
        seen.add(cur)
        st = states[cur]
        payload = (st.get("Parameters") or {}).get("Payload") or {}
        if payload.get("stage") and payload.get("task"):
            count += 1
        if st["Type"] != "Choice":
            cur = st.get("Next")
            continue
        passing = [ch["Next"] for ch in st["Choices"]
                   if ch.get("Variable", "").endswith("gate_passed")
                   and ch.get("BooleanEquals") is True]
        cur = passing[0] if passing else st.get("Default")
    return count


def test_the_documented_happy_path_state_count_matches_the_state_machine():
    """Both ARCHITECTURE variants said 8; MonitorHealth and MonitorReport made it 10.

    A wrong count here is not cosmetic: it is how the monitor harness stayed undispatched
    for the platform's whole life while the docs described it as a stage. The prose was
    read as evidence that the wiring existed.
    """
    n = _happy_path_harness_states()
    docs = {"ARCHITECTURE.md": r"\*\*(\d+) harness-task states",
            "ARCHITECTURE.zh-TW.md": r"\*\*(\d+) 個 harness 任務狀態"}
    for name, pattern in docs.items():
        text = (REPO / "docs" / name).read_text()
        claims = re.findall(pattern, text)
        assert claims, f"{name}: no harness-task-state count claim left to check"
        for c in claims:
            assert int(c) == n, f"{name} claims {c} happy-path harness states, the ASL has {n}"


def test_the_documented_state_and_lambda_counts_match_the_deployers():
    """PROJECT_STATE's infrastructure table is the one-screen answer to "what is running".

    It said 9 states and 5 Lambdas while the ASL had 24 and LAMBDAS had 6. Both drifted by
    addition, which is the only way this table ever goes wrong and the way no reader can
    detect: a number that was once measured looks measured forever.
    """
    text = (REPO / "PROJECT_STATE.md").read_text()
    n_states = len(json.loads(
        (REPO / "orchestration/state_machine.asl.json").read_text())["States"])
    claimed = re.findall(r"\| (\d+) states", text)
    assert claimed, "PROJECT_STATE.md no longer states a state count"
    for c in claimed:
        assert int(c) == n_states, f"PROJECT_STATE claims {c} states, the ASL has {n_states}"

    lambdas = (REPO / "deploy/07_lambdas.py").read_text()
    n_fns = len(re.findall(r'^\s{8}"fn": "llmops-', lambdas, re.M))
    assert n_fns, "could not count LAMBDAS entries -- did the table's shape change?"
    claimed_fns = re.findall(r"\| Lambdas ×(\d+)", text)
    assert claimed_fns, "PROJECT_STATE.md no longer states a Lambda count"
    for c in claimed_fns:
        assert int(c) == n_fns, f"PROJECT_STATE claims ×{c} Lambdas, LAMBDAS has {n_fns}"


def test_every_schedule_the_deployer_creates_is_named_in_the_cost_posture():
    """The cost posture called the finops reconcile "one recurring cost" and "the only
    schedule enabled by default". The sweep schedule made both false the moment it landed.

    This is the doc claim with money attached: a reader deciding whether this platform is
    safe to leave running reads this paragraph and nothing else. A schedule that is ENABLED
    by default and absent from it is a standing charge nobody was told about.
    """
    triggers = (REPO / "deploy/08_triggers.py").read_text()
    names = set(re.findall(r'^[A-Z_]*SCHEDULE_NAME = "([a-z-]+)"', triggers, re.M))
    assert names, "no schedule names found in 08_triggers.py"
    # A schedule created DISABLED by default is not a standing cost, so it need not appear.
    enabled = {n for n in names if n != "llmops-nightly"}
    text = (REPO / "PROJECT_STATE.md").read_text()
    posture = text.split("## Standing cost posture")[1].split("\n## ")[0]
    for name in sorted(enabled):
        stem = name.replace("llmops-", "").replace("-daily", "").replace("-", " ")
        assert name in posture or stem in posture, (
            f"{name} is ENABLED by default and the standing cost posture never mentions "
            f"it; the paragraph a reader uses to decide what this platform costs to leave "
            "running has to name every schedule that runs on its own")
