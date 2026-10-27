"""Every number the Introduction speaks or shows, re-derived from the file it comes from.

narration.json said this file existed from 2026-08 ("tests/test_intro_narration.py re-derives each
one"); it never did, so for three months nothing checked a single spoken number. A voice-over is
the least checkable surface a project has: a reader cannot diff audio, and a wrong number baked
into an MP3 costs a five-language Polly run to correct. Hence the derivations, the same numbers in
all five languages, the numerals the scenes display, and the owner's standing instruction (never
frame the agents as replacing people) checked in every language.
"""
from __future__ import annotations

import glob
import json
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
INTRO = REPO / "deploy/console/intro"
NARR = json.loads((INTRO / "narration.json").read_text(encoding="utf-8"))
TEMPLATE = (INTRO / "page.template.html").read_text(encoding="utf-8")
EN = " ".join(NARR["text"]["en"].values())
EVID = REPO / "deploy/evidence"


def _states():
    return len(json.loads((REPO / "orchestration/state_machine.asl.json").read_text())["States"])


def _lambdas():
    src = (REPO / "deploy/07_lambdas.py").read_text()
    block = src.split("LAMBDAS = {", 1)[1].split("\n}\n", 1)[0]
    return len(re.findall(r'^    "([a-z_]+)": \{', block, re.M))


def _harnesses():
    return len(glob.glob(str(REPO / "agents/*/harness.json")))


def _skill_mounts():
    n, kinds = 0, set()
    for cfg in glob.glob(str(REPO / "agents/*/harness*.json")):
        for s in json.load(open(cfg)).get("skills") or []:
            n += 1
            kinds |= {k for k in ("git", "s3") if k in s}
    return n, kinds


_WORDS = {7: "seven", 8: "eight", 19: "nineteen", 39: "thirty nine", 30: "thirty"}
#: the same numbers as each language spells them for its voice
_SPELLED = {
    "states": {"zh": "三十九", "yue": "三十九", "ja": "三十九", "ko": "서른아홉"},
    "lambdas": {"zh": "八个 Lambda", "yue": "八個 Lambda", "ja": "八つの Lambda", "ko": "여덟 개의 Lambda"},
    "agents": {"zh": "七个 agent", "yue": "七個 agent", "ja": "七つのエージェント", "ko": "일곱 개의 에이전트"},
    "mounts": {"zh": "十九个技能挂载", "yue": "十九個技能掛載", "ja": "十九のスキル", "ko": "열아홉 개의 스킬"},
    "answer_match": {"zh": "百分之七十二点五", "yue": "百分之七十二點五", "ja": "七十二点五パーセント", "ko": "칠십이 점 오 퍼센트"},
    "held_out": {"zh": "一百二十", "yue": "一百二十", "ja": "百二十", "ko": "백이십"},
    "pii": {"zh": "三十处", "yue": "三十處", "ja": "三十か所", "ko": "서른 곳"},
    "cost": {"zh": "四十五美分", "yue": "四十五美仙", "ja": "四十五セント", "ko": "사십오 센트"},
}


def _scene_texts(lang):
    return " ".join(NARR["text"][lang].values())


# ── the live system ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("key,derive,phrase", [
    ("states", _states, "states"), ("lambdas", _lambdas, "Lambdas"), ("agents", _harnesses, "agents")])
def test_counts_spoken_match_the_system(key, derive, phrase):
    n = derive()
    assert f"{_WORDS[n]} {phrase}" in EN, (
        f"the system has {n} {phrase}; the English narration does not say '{_WORDS[n]} {phrase}'")
    for lang, spelled in _SPELLED[key].items():
        assert spelled in _scene_texts(lang), f"{lang} narration lost '{spelled}' ({n} {phrase})"


def test_the_skill_mounts_spoken_are_the_configured_ones():
    n, kinds = _skill_mounts()
    assert kinds == {"s3"} and f"{_WORDS[n].capitalize()} skill mounts" in EN and "pinned copy in S3" in EN
    for lang, spelled in _SPELLED["mounts"].items():
        assert spelled in _scene_texts(lang)


def test_the_displayed_counts_match_the_system():
    assert f"{_states()} states" in TEMPLATE and f"{_lambdas()} Lambdas" in TEMPLATE
    n, _ = _skill_mounts()
    assert f"{n} mounts" in TEMPLATE and f"{n} skill mounts" in TEMPLATE


def test_the_config_shown_is_the_model_the_harnesses_run():
    ids = {json.load(open(c))["model"]["bedrockModelConfig"]["modelId"]
           for c in glob.glob(str(REPO / "agents/*/harness.json"))}
    assert len(ids) == 1, f"the harnesses run {ids}; the scene shows one model"
    assert f'"{next(iter(ids))}"' in TEMPLATE, "s4 shows a model id no harness runs"


# ── the measured results ─────────────────────────────────────────────────────
def test_the_m1_scores_are_the_recorded_ones():
    m1 = (EVID / "VERIFICATION_m1_lakehouse_e2e.md").read_text(encoding="utf-8")
    assert "**0.0%** (0/120" in m1 and "**72.5%** (87/120" in m1 and "**39.2%** (47/120" in m1
    assert "seventy-two point five percent" in EN and "one hundred and twenty held-out" in EN
    assert "0% → <span" in TEMPLATE and "72.5%" in TEMPLATE and "39.2% → 100%" in TEMPLATE
    for key in ("answer_match", "held_out"):
        for lang, spelled in _SPELLED[key].items():
            assert spelled in _scene_texts(lang), f"{lang} lost '{spelled}'"


def test_the_identifier_results_are_the_recorded_ones():
    ig = (EVID / "VERIFICATION_identifier_grounding.md").read_text(encoding="utf-8")
    assert "**0/120**" in ig and "**120/120**" in ig and "85/120 = 70.8%" in ig
    assert "in all 234 the assistant" in ig, "the 234/234 training-data claim lost its source"
    assert "0/120" in TEMPLATE and "120/120" in TEMPLATE and "70.8%" in TEMPLATE and "234 / 234" in TEMPLATE


def test_the_pii_count_and_the_cost_are_the_recorded_ones():
    pc = (EVID / "VERIFICATION_provenance_card.md").read_text(encoding="utf-8")
    assert "`data.pii_redactions` 30" in pc and "`run.cost_usd` 0.45" in pc
    assert "thirty pieces of personal data" in EN and "forty-five cents" in EN
    assert "30 pieces of personal data" in TEMPLATE and "$0.45" in TEMPLATE
    for key in ("pii", "cost"):
        for lang, spelled in _SPELLED[key].items():
            assert spelled in _scene_texts(lang), f"{lang} lost '{spelled}'"


def test_the_approval_spoken_is_the_recorded_one():
    ar = (EVID / "VERIFICATION_architecture_refresh.md").read_text(encoding="utf-8")
    assert "v1 `Rejected`, v2 `Approved`" in ar
    assert "rejected the first and approved the second" in EN
    assert "version 1 · rejected" in TEMPLATE and "version 2 · approved" in TEMPLATE


# ── the owner's instruction, in every language ───────────────────────────────
_REPLACING = {"en": ("hold the pager", "glue work", "on call"), "zh": ("pager", "胶水", "值班"),
              "yue": ("pager", "膠水", "候命"), "ja": ("ポケベル", "配管作業", "オンコール"),
              "ko": ("호출기", "접착 작업", "온콜")}


@pytest.mark.parametrize("lang", sorted(_REPLACING))
def test_no_language_frames_the_agents_as_replacing_people(lang):
    said = _scene_texts(lang) + " ".join(NARR["titles"][lang].values())
    for phrase in _REPLACING[lang]:
        assert phrase not in said, f"{lang} says {phrase!r}: the agents are framed as replacing people"


def test_the_narration_comment_names_this_file_and_it_exists():
    comment = " ".join(NARR["_comment"])
    assert "tests/test_intro_narration.py" in comment and pathlib.Path(__file__).exists()
