"""The console's Architecture page says what the system is, derived from the files that make it so.

The page went three months without following the design: its diagram had no lakehouse, no
Unified Studio, no provenance card and no canonical scorer, and its caption said the skills were
"mounted from git" after every mount had become s3 (2026-10-27 review). Each claim the refreshed
page makes is pinned here to the source it comes from, so the next change to that source fails a
test instead of quietly ageing the page.
"""
from __future__ import annotations

import glob
import json
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]
HIGH = (REPO / "docs/architecture-high-level.svg").read_text(encoding="utf-8")
FRONT = (REPO / "deploy/console/frontend.html").read_text(encoding="utf-8")


def _band_titles(svg: str) -> list[str]:
    return re.findall(r'<text class="bandT"[^>]*>([^<]+)</text>', svg)


def test_the_diagram_draws_all_four_planes():
    titles = " | ".join(_band_titles(HIGH))
    for plane in ("AUDIT PLANE", "OPERATIONS", "DATA PLANE", "GOVERNANCE PLANE"):
        assert plane in titles, f"the high-level diagram lost its {plane} band: {titles}"


def test_the_data_plane_lists_the_silver_rules_that_run():
    src = (REPO / "pipeline/lakehouse/transform.py").read_text(encoding="utf-8")
    block = src.split("report = {r: [] for r in (", 1)[1].split(")}", 1)[0]
    rules = re.findall(r'"([a-z_]+)"', block)
    assert "strip_ungrounded_ids" in rules, "the derivation no longer finds the rule list"
    line = next(l for l in HIGH.splitlines() if "Silver rules:" in l)
    for rule in rules:
        assert rule in line, f"Silver runs {rule} but the diagram does not say so"


def test_the_governance_plane_states_the_live_state_count():
    states = len(json.loads((REPO / "orchestration/state_machine.asl.json").read_text())["States"])
    assert f"({states} states)" in HIGH


def test_every_governance_card_links_a_file_that_exists():
    for target in ("pipeline/eval/identifier_grounding.py", "orchestration/publish_provenance/handler.py",
                   "pipeline/contracts/provenance.py", "deploy/11_studio.py",
                   "pipeline/lakehouse/export.py", "pipeline/lakehouse/transform.py"):
        assert f"blob/main/{target}" in HIGH, f"no card links {target}"
        assert (REPO / target).is_file(), f"a card links {target}, which does not exist"


def test_the_page_caption_names_the_real_skill_source_kind():
    """The caption is prose in frontend.html, so it is checked against the configs the same way
    the diagram's STATE band is (test_docs_claims::test_the_diagram_text_states_the_real_skill_source_kind)."""
    kinds = set()
    for cfg in glob.glob(str(REPO / "agents/*/harness*.json")):
        for skill in json.load(open(cfg)).get("skills") or []:
            kinds |= {k for k in ("git", "s3") if k in skill}
    caption = FRONT.split('<div id="archLow">', 1)[1].split("</div>\n  </div>", 1)[0]
    if kinds == {"s3"}:
        assert "pinned S3 mirror" in caption and "mounted from git" not in caption
    else:
        assert "mounted from git" in caption, f"the configs mount {kinds}; the caption must say so"


def test_the_page_says_the_pipeline_never_approves():
    """The claim is true because the publisher's role denies it; the page and the role agree."""
    role = json.loads((REPO / "deploy/iam/lambda_roles.json").read_text())["roles"]["publish_provenance"]
    assert any(s.get("Sid") == "NeverApprove" and s["Effect"] == "Deny"
               for s in role["permissionsPolicy"]["Statement"])
    assert "The pipeline never approves" in FRONT and "the pipeline never approves" in HIGH


def test_both_architecture_docs_carry_the_planes_and_the_scorer_sections():
    en = (REPO / "docs/ARCHITECTURE.md").read_text(encoding="utf-8")
    zh = (REPO / "docs/ARCHITECTURE.zh-TW.md").read_text(encoding="utf-8")
    assert "## 0. Four planes" in en and "## 0. 四個平面" in zh
    assert "### 7d. Canonical scorers" in en and "### 7d. Canonical scorer" in zh
