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

import ast
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
    """The docs state 19 sources and which KIND they are. Derive both from the configs.

    Stating "19 git, 0 s3" in prose is exactly the kind of claim that was false here
    before, so it is checked against the files. It has now been through the migration it
    was written to catch: the sources moved to s3 and this test failed and named the new
    numbers, which is what forced the eight docs to move in the same commit.

    Both halves are asserted, because either one alone can be true while the sentence as
    a whole misleads:
      * the COUNT, against the total number of sources -- not against the git total. Read
        against git alone it would go green the moment the last git source disappeared and
        the docs still said 19, since 19 != 0 would be the only thing that failed and a
        doc dropping the number entirely is caught by the `silent` check above, not here.
      * the KIND named next to the count. A doc left saying "19 sources, all `git`" is
        precisely as wrong as one saying 4, and the count check cannot see it.
    """
    counts = _skill_sources()
    git_n = sum(k.get("git", 0) for k in counts.values())
    s3_n = sum(k.get("s3", 0) for k in counts.values())
    total = git_n + s3_n
    want_kind = "s3" if s3_n else "git"
    claims = []
    for doc in DOC_SKILL_CLAIMS:
        text = doc.read_text()
        # The lookbehind is load-bearing: without it the phrase "the s3 sources work"
        # parses as a claim of THREE sources, and the guard fails on prose that states no
        # count at all. Any count claim is preceded by whitespace or start-of-line.
        for pattern in (r"(?<![A-Za-z0-9_])(\d+)\s+(?:skill sources|sources)",
                        r"(?<![A-Za-z0-9_])(\d+)\s*個(?:技能)?來源"):
            for m in re.finditer(pattern, text):
                # The kind is whichever of git/s3 is named FIRST after the count: the
                # corrected prose reads "are `s3` today; none are `git`", so merely
                # looking for the expected token in the window would also accept the
                # sentence with the two swapped.
                after = text[m.end():m.end() + 160]
                kinds = re.findall(r"`(git|s3)`", after)
                claims.append((doc.name, int(m.group(1)), kinds[0] if kinds else None))
    # Per-doc, not "at least one doc". Requiring only one leaves the guard satisfied
    # while the count is quietly deleted from the other seven -- the check would then be
    # anchored to whichever file still happens to mention it. Each doc in the list either
    # states the count or is not in the list.
    stating = {name for name, _, _ in claims}
    silent = [d.name for d in DOC_SKILL_CLAIMS if d.name not in stating]
    assert not silent, (
        f"these docs no longer state a skill-source count: {silent}. The counts were "
        "wrong before, so dropping them removes the check rather than passing it. Either "
        "state the count or remove the file from DOC_SKILL_CLAIMS deliberately.")
    wrong = [f"{name} says {n}, configs have {total} skill sources"
             for name, n, _ in claims if n != total]
    assert not wrong, "; ".join(wrong) + f" (per-config: {counts})"
    miskind = [f"{name} calls the {n} sources {k!r}" for name, n, k in claims
               if k != want_kind]
    assert not miskind, (
        "; ".join(miskind) + f", but the configs have {git_n} git and {s3_n} s3. "
        "A count that is right about the number and wrong about the kind still tells the "
        "reader the migration never happened.")
    assert s3_n == 0 or git_n == 0, (
        f"skill sources are now MIXED ({git_n} git, {s3_n} s3): {counts}. A partial "
        "migration means some harnesses read a pinned snapshot and others float on the "
        "skill repo's default branch, which is the drift the migration exists to stop. "
        "Update the docs and this guard together.")


def test_the_diagram_text_states_the_real_skill_source_kind():
    """The high-level SVG's STATE band names the mount count and kind. Derive both.

    This guard exists because the drawing outlived two corrections of the same sentence.
    It first claimed "git in dev, S3 mirror in prod"; corrected to "all 19 mounts are
    `git`, the mirror exists but nothing is switched to it" -- true when written -- and
    then the migration it described made that false too, so the band went on asserting
    `git` with the correction's own prose vouching for it.

    Two independent evasions let that happen, and both are closed here:
      * test_the_skill_source_claims_match_the_harness_configs scans .md files only, and
        the band lives in an .svg;
      * it looks for "N sources", and the band says "N mounts".
    A sentence one token away from a checked sentence is unchecked.

    The claim is located by parsing the band's own <text> element, not by searching the
    60 KB file. An unanchored substring search over a document that repeats every label
    passes on any incidental hit -- the lesson from the negative control whose "sweep"
    anchor matched prose it was not aiming at.
    """
    svg = (REPO / "docs" / "architecture-high-level.svg").read_text()
    lines = [l for l in svg.splitlines()
             if re.match(r'\s*<text class="sub"', l) and "skills mounted from" in l]
    assert len(lines) == 1, (
        f"expected exactly one skill-mount line in the STATE band, found {len(lines)}. "
        "Regenerate with docs/gen_architecture_svg.py; the band is generated, never "
        "hand-edited.")
    band = lines[0]

    counts = _skill_sources()
    git_n = sum(k.get("git", 0) for k in counts.values())
    s3_n = sum(k.get("s3", 0) for k in counts.values())
    total = git_n + s3_n
    assert total, f"no skill sources found in any harness config: {counts}"

    m = re.search(r"all (\d+) mounts across all (\d+) harnesses are ([a-z0-9+ ]+?):", band)
    assert m, (
        "the STATE band no longer states its mount count, harness count and source kind "
        f"in the form the generator emits: {band.strip()!r}. Dropping the claim removes "
        "this check rather than passing it.")
    claimed_mounts, claimed_harnesses, claimed_kind = int(m.group(1)), int(m.group(2)), m.group(3)

    assert claimed_mounts == total, (
        f"the diagram says {claimed_mounts} skill mounts, the configs have {total}: {counts}")
    n_harnesses = len(list((REPO / "agents").glob("*/harness.json")))
    assert claimed_harnesses == n_harnesses, (
        f"the diagram says {claimed_harnesses} harnesses, agents/ has {n_harnesses}")

    # The kind is asserted separately from the count for the reason the .md guard gives:
    # a band right about 19 and wrong about `git` tells the reader the migration never
    # happened, and the count check cannot see it.
    want_kind = "s3" if s3_n and not git_n else "git" if git_n and not s3_n else None
    assert want_kind is not None, (
        f"skill sources are MIXED ({git_n} git, {s3_n} s3): {counts}. The generator emits "
        "a '3 git+16 s3' description for this case rather than a majority kind; update "
        "this guard deliberately if a mixed fleet becomes the intended steady state.")
    assert claimed_kind == want_kind, (
        f"the diagram calls the {total} mounts {claimed_kind!r}, but the configs have "
        f"{git_n} git and {s3_n} s3. This is the exact sentence that decayed twice.")

    # And the operational clause has to move with the kind. "are s3" followed by prose
    # about reading GitHub at session start is a sentence that contradicts its own
    # subject, which is how a half-updated correction reads as a whole one.
    reaches_github = "reads GitHub at session start" in band or "read from GitHub" in band
    if want_kind == "s3":
        assert "pinned snapshot" in band and "no harness reads GitHub" in band, (
            f"the band names s3 but does not say what that means operationally: {band.strip()!r}")
    else:
        assert reaches_github, (
            f"the band names git but does not say the harnesses read GitHub: {band.strip()!r}")


#: Docs whose §2 states how many harness-task states the happy path has, and names them.
DOC_SPINE_CLAIMS = (REPO / "docs" / "ARCHITECTURE.md",
                    REPO / "docs" / "ARCHITECTURE.zh-TW.md")


def _happy_path_harness_states() -> list[str]:
    """Walk the ASL from the full-pipeline entry to Complete, gate PASSING every time.

    Derived rather than listed because the list is exactly what drifted: `EvalGenerate`
    was inserted between `FinetuneAnalyze` and `EvalGate` and both ARCHITECTURE variants
    went on saying 8 states and drawing analyze → gate. A hardcoded expectation here
    would need the same edit as the prose, so it would drift in the same commit and
    guard nothing.

    "Gate passing" is what makes this the HAPPY path: taking the Choice's pass branch
    skips remediation, so the walk terminates instead of looping.
    """
    asl = json.loads((REPO / "orchestration" / "state_machine.asl.json").read_text())
    states = asl["States"]
    node, order, seen = states["PipelineModeChoice"]["Default"], [], set()
    while node and node not in seen:
        seen.add(node)
        st = states[node]
        payload = (st.get("Parameters") or {}).get("Payload") or {}
        if "harness_id" in payload:
            order.append(node)
        if st["Type"] == "Choice":
            # The happy path is the branch that leads onward, not back into remediation.
            nxt = next((c["Next"] for c in st["Choices"]
                        if c.get("BooleanEquals") is True), st.get("Default"))
        else:
            nxt = st.get("Next")
        node = nxt
    assert len(order) >= 5, f"walked only {order} -- the traversal is broken, not the docs"
    return order


def test_the_documented_spine_matches_the_state_machine():
    """ARCHITECTURE §2's count AND its diagram must come from the ASL.

    Two separate claims, both checkable, and the second is the one that matters: a
    reader who trusts the arrow `FinetuneAnalyze → EvalGate` concludes the gate is
    evaluated by whatever analysis produced, i.e. that no separate generation step
    exists. That was true of the DIAGRAM and false of the pipeline's intent for as long
    as `evaluate` was declared and dispatched by nothing. So the state names are checked
    in ORDER, not merely for presence -- a diagram may not draw a hop that the machine
    does not have, nor omit one it does.
    """
    order = _happy_path_harness_states()
    for doc in DOC_SPINE_CLAIMS:
        text = doc.read_text()
        assert re.search(rf"\*\*{len(order)} (?:harness-task states|個 harness 任務狀態)",
                         text), (
            f"{doc.name} §2 does not state the happy path's harness-task state count "
            f"({len(order)}: {order}) in the form this guard reads. Deleting the phrase "
            "removes the check rather than satisfying it.")
        # The arrows are ASCII, so positions in the raw text ARE the drawn order.
        at = [text.find(n) for n in order]
        missing = [n for n, i in zip(order, at) if i < 0]
        assert not missing, (
            f"{doc.name} never names these happy-path states: {missing}. A stage absent "
            "from the diagram reads as a stage that does not exist.")
        assert at == sorted(at), (
            f"{doc.name} draws the spine as "
            f"{[n for _, n in sorted(zip(at, order))]} but the ASL runs it as {order}. "
            "An out-of-order diagram misstates which state's output the next one reads.")

#: Docs that describe which model each harness runs.
DOC_MODEL_CLAIMS = (REPO / "docs" / "ARCHITECTURE.md", REPO / "docs" / "ARCHITECTURE.zh-TW.md")

_MODEL_ID = re.compile(r"global\.anthropic\.claude-[a-z0-9.-]+")


def _configured_models() -> dict[str, str]:
    """modelId per harness config, read from the file the deployer actually applies."""
    out = {}
    for cfg in sorted((REPO / "agents").glob("*/harness.json")):
        model = json.loads(cfg.read_text()).get("model") or {}
        out[str(cfg.relative_to(REPO))] = (
            model.get("bedrockModelConfig") or {}).get("modelId")
    return out


def test_the_model_allocation_claim_matches_the_harness_configs():
    """§9 item 3 said the premium/fallback split was in production. It never was.

    Same failure mode as the skill-source claim above and the §11 VPC claim: a designed
    lever read back as a delivered feature. All 7 configs carry one model id, and
    `GetHarness` agreed when checked live, so the split is available -- not deployed.
    Derive the fleet's shape from the configs, then require the docs to state THAT:

      * the uniform model id and the harness count both come from the files;
      * any OTHER model id the docs name must sit in a paragraph that marks it
        undeployed or a fallback, so "orchestrator runs Opus 5" cannot be reasserted
        as fact without failing here.
    """
    models = _configured_models()
    assert models, "no agents/*/harness.json found"
    missing = [p for p, m in models.items() if not m]
    assert not missing, f"these harness configs declare no modelId: {missing}"
    distinct = sorted(set(models.values()))
    assert len(distinct) == 1, (
        f"harness model ids are no longer uniform: {models}. That is a real change of "
        "state, not a test failure -- §9 item 3 in BOTH language variants says the mixed "
        "allocation is a lever and not deployed, so update the prose and this guard "
        "together, in the same commit.")
    deployed = distinct[0]

    # The sentence that carries the claim, per language, with both numbers derived.
    patterns = {
        "ARCHITECTURE.md": re.compile(
            rf"[Aa]ll (\d+) live\s+harnesses run `{re.escape(deployed)}`"),
        "ARCHITECTURE.zh-TW.md": re.compile(
            rf"(\d+) 個 harness 全部運行 `{re.escape(deployed)}`"),
    }
    for doc in DOC_MODEL_CLAIMS:
        text = doc.read_text()
        m = patterns[doc.name].search(text)
        assert m, (
            f"{doc.name} no longer states which model the fleet runs in the form this "
            f"guard checks (expected the deployed id {deployed!r} next to a harness "
            "count). Deleting the sentence removes the check rather than passing it.")
        assert int(m.group(1)) == len(models), (
            f"{doc.name} says {m.group(1)} harnesses run {deployed}; there are "
            f"{len(models)} configs: {sorted(models)}")

    # A non-deployed model id may only be named as a lever, never as current state.
    lever = ("not what is deployed", "fallback", "designed", "not a state",
             "後備", "尚未", "不是", "設計")
    stray = []
    for doc in DOC_MODEL_CLAIMS:
        for para in re.split(r"\n\s*\n", doc.read_text()):
            others = {i for i in _MODEL_ID.findall(para) if i != deployed}
            if others and not any(mark in para for mark in lever):
                stray.append(f"{doc.name}: {sorted(others)} asserted without marking it "
                             f"undeployed, in: {para.strip()[:120]!r}")
    assert not stray, "; ".join(stray)


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

def _happy_path_harness_state_count() -> int:
    """Harness-task states a successful default-mode run passes through.

    Named apart from `_happy_path_harness_states` above deliberately. Both branches of
    this merge wrote a guard for the same claim and gave the helper the same name, so the
    auto-merge kept both bodies and Python kept only the SECOND -- silently disabling the
    other test's derivation while the suite stayed green. Two independent walks that agree
    are worth more than one (this one starts at StartAt and keys on `stage`+`task`; the
    other starts at PipelineModeChoice's Default and keys on `harness_id`), so both are
    kept, under distinct names.

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
    n = _happy_path_harness_state_count()
    docs = {"ARCHITECTURE.md": r"\*\*(\d+) harness-task states",
            "ARCHITECTURE.zh-TW.md": r"\*\*(\d+) 個 harness 任務狀態"}
    for name, pattern in docs.items():
        text = (REPO / "docs" / name).read_text()
        claims = re.findall(pattern, text)
        assert claims, f"{name}: no harness-task-state count claim left to check"
        for c in claims:
            assert int(c) == n, f"{name} claims {c} happy-path harness states, the ASL has {n}"


#: How each doc states the Lambda count. PROJECT_STATE writes it in an infrastructure
#: table cell, the READMEs in a repo-map line -- so no single regex covers all three, and
#: a guard that only knows the table form is exactly the guard that let both READMEs say
#: 5 for 21 merged PRs while passing. The file it watches is also the one file that gets
#: fixed, because it is the one the failure names.
LAMBDA_COUNT_PATTERNS = {
    "PROJECT_STATE.md": r"\| Lambdas ×(\d+)",
    "README.md": r"state machine \+ (\d+) Lambdas",
    "README.zh-TW.md": r"狀態機 \+ (\d+) 個 Lambda",
}


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

    # The same number, everywhere it is stated -- and each doc's own phrasing, because a
    # regex that matches none of them would pass every file vacuously.
    for name, pattern in LAMBDA_COUNT_PATTERNS.items():
        doc = (REPO / name).read_text()
        stated = re.findall(pattern, doc)
        assert stated, f"{name} no longer states a Lambda count as /{pattern}/"
        for c in stated:
            assert int(c) == n_fns, (
                f"{name} claims {c} Lambdas, deploy/07_lambdas.py deploys {n_fns}")

    # The digit alone is a claim a reader cannot check, and the list beside it is what
    # actually went stale: the READMEs named four functions and omitted monitor-sweep.
    fn_names = re.findall(r'^\s{8}"fn": "llmops-([a-z-]+)"', lambdas, re.M)
    for name in ("README.md", "README.zh-TW.md"):
        line = next(l for l in (REPO / name).read_text().splitlines()
                    if re.search(LAMBDA_COUNT_PATTERNS[name], l))
        listed = re.search(r"[(（]([^)）]*)[)）]", line)
        assert listed, f"{name}'s Lambda line names no functions at all"
        items = [i.strip() for i in listed.group(1).split("/")]
        assert len(items) == n_fns, (
            f"{name} lists {len(items)} Lambda names {items} beside the digit {n_fns}")
        # Match on segments, not full names: the list is deliberately abbreviated
        # (harness-driver -> driver), so demanding the deployed name verbatim would fail
        # on a correct line and teach the next reader to delete the check.
        for fn in fn_names:
            assert any(seg in items for seg in fn.split("-")) or fn in items, (
                f"{name}'s Lambda list {items} names nothing for deployed llmops-{fn}")


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


def test_the_documented_negative_control_count_matches_the_runner():
    """"Mutation-checked" is an adjective, and an adjective cannot go stale.

    TEST_RESULTS said "every guard added in this work was mutation-checked" and stated no
    number, so a control silently deleted -- or a guard added with no control at all --
    left the sentence still reading true. The claim only becomes checkable once it carries
    the count, and the count only stays true once something derives it.

    Two numbers are derived, because they answer different questions and have drifted
    apart before: how many mutations exist (``case(...)`` registrations) and how many
    (guard, mutation) pairs they assert (the test ids listed inside them). The runner
    prints one PASS line per pair, so the pair count is what a reader comparing the doc to
    the runner's output actually sees.
    """
    src = (REPO / "tests/negative_controls/monitor_dispatch.py").read_text()
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "case"]
    assert calls, "no case(...) registrations found -- did the runner's shape change?"
    n_cases = len(calls)
    # args[3] is the list of pytest node ids this mutation must break. A case that lists
    # none is a mutation nothing verifies, which is the defect this guard exists to name.
    n_pairs = 0
    for c in calls:
        listed = c.args[3]
        assert isinstance(listed, ast.List) and listed.elts, (
            f"a case(...) at line {c.lineno} names no test to break; a mutation with no "
            "guard listed is a control that cannot fail")
        n_pairs += len(listed.elts)

    count = re.compile(r"\*\*(\d+)/(\d+)\s*(?:negative controls|反向控制)\*\*")
    for doc in DOCS:
        text = doc.read_text()
        stated = count.findall(text)
        assert stated, (
            f"{doc.name} states no negative-control count; 'mutation-checked' with no "
            "number is a claim that stays true while controls disappear")
        # The count has to be IN the mutation-check sentence, not merely somewhere in the
        # file. Anchoring on the whole document was not enough: this guard's own negative
        # control stripped the number out of that sentence -- restoring the bare adjective
        # this test exists to forbid -- and the check still passed on the summary table's
        # row further up. The sentence a reader takes the claim from is the sentence that
        # has to carry the number.
        claim = next((para for para in text.split("\n\n")
                      if "mutation-check" in para or "mutation check" in para), None)
        assert claim, f"{doc.name} no longer says the guards were mutation-checked at all"
        assert count.search(claim), (
            f"{doc.name}'s mutation-check claim states no count:\n{claim}\nAn adjective "
            "cannot go stale, which is exactly why it is not evidence.")
        for passed, total in stated:
            assert int(total) == n_pairs, (
                f"{doc.name} claims {total} negative controls, the runner registers "
                f"{n_cases} mutations asserting {n_pairs} (guard, mutation) pairs")
            assert passed == total, (
                f"{doc.name} claims {passed}/{total} controls passing; a documented "
                "result with a failing control in it is not evidence of anything")


def test_the_shell_suite_is_documented_with_its_assertion_count():
    """CI runs ``tests/*.sh``; the pytest-derived count guard cannot see a single one.

    ``test_capacity_race_guard.sh`` landed with 10 assertions and CI has run them on every
    push since, while both TEST_RESULTS variants reported only the pytest total -- so the
    documented evidence understated what was actually verified. The count guard could not
    have caught it: it derives from ``pytest --collect-only``, and a shell suite is not a
    pytest test. A second suite therefore needs a second derivation.

    The derivation runs the suite and reads the total it prints, rather than counting
    ``check`` invocations in its source. Counting the source gave 9 for a suite that
    asserts 10: the most expensive case -- "the winner is never among the stopped" -- is
    written inline with its own PASS/FAIL branch instead of through the helper. A guard
    that is wrong for its own reasons would have had the docs corrected to a false number.
    The suite is offline by construction (a mock ``guard.sh`` over files in a temp dir),
    so running it here costs nothing the rest of the suite does not already assume.
    """
    suites = sorted((REPO / "tests").glob("*.sh"))
    assert suites, "no shell suites found -- has CI's tests/*.sh loop got nothing to run?"
    for suite in suites:
        proc = subprocess.run(["bash", str(suite)], capture_output=True, text=True,
                              cwd=REPO, timeout=300)
        m = re.search(r"=== (\d+) passed, (\d+) failed ===", proc.stdout)
        assert m, (f"{suite.name} printed no '=== N passed, N failed ===' total; "
                   f"rc={proc.returncode}\nstdout tail:\n{proc.stdout[-2000:]}")
        n, failed = int(m.group(1)), int(m.group(2))
        assert failed == 0 and proc.returncode == 0, (
            f"{suite.name}: {failed} assertions failed\n{proc.stdout[-2000:]}")
        for doc in DOCS:
            text = doc.read_text()
            assert suite.name in text, (
                f"{doc.name} never names {suite.name}, which CI runs on every push; "
                "a suite absent from the evidence file reads as a suite that does not exist")
            # Deliberately NOT "N/N passed": that is _CLAIM's form, and _CLAIM asserts
            # every match equals the pytest collection total. A shell suite's 10 written
            # in pytest's phrasing would be read as a stale pytest count and "fixed" to
            # match the suite it is not part of.
            m = re.search(re.escape(suite.name) + r"[^|\n]*\|[^|\n]*?\*\*(\d+)/(\d+)"
                          r"\s*(?:assertions|斷言)\*\*", text)
            assert m, (f"{doc.name} names {suite.name} but states no N/N assertion "
                       "count in the same table row")
            assert int(m.group(2)) == n, (
                f"{doc.name} claims {m.group(2)} assertions for {suite.name}, it makes {n}")
            assert m.group(1) == m.group(2), (
                f"{doc.name} claims {m.group(1)}/{m.group(2)} for {suite.name}")
