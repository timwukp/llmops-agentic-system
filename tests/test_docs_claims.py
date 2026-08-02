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
