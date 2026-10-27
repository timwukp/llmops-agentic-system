"""identifier_grounding: the scorer that catches an answer stating an identifier the question never gave.

Offline. The live finding it encodes: on 2026-10-23 every fine-tuned answer ended in an invented case
number ("（案件編號 C00089）"), and answer_match, a containment check, scored them as matches; this
scorer gives that run 0/120. Pinned here: what counts as an identifier (CJK text around it included),
what grounds one, the Wilson bounds the gate decides on, and that the eval prompt and the demo preset
use this scorer and a threshold a 120-item set can actually pass.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("identifier_grounding", REPO / "pipeline/eval/identifier_grounding.py")
ig = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ig)


@pytest.mark.parametrize("prompt,answer,bad", [
    # the live shapes, both languages
    ("電動牙刷已經是週年慶價了，點數還能再扣嗎？", "可以，可以同時使用。（案件編號 C00089）", ["C00089"]),
    ("TW10232: the toothbrush is faulty.", "A return is filed. (case C00148)", ["C00148"]),
    # an order number the customer gave is grounded; the invented case number beside it is not
    ("Any update on where TW10305 is?", "Your order TW10305 has shipped. (case C00076)", ["C00076"]),
    # no space between CJK and the identifier: \b would miss it
    ("查詢訂單", "已登記，編號C00089。", ["C00089"]),
    ("查詢訂單", "Ref SKU-0042 and TW10001.", ["SKU-0042", "TW10001"]),
    # nothing to flag
    ("Is the backpack in stock?", "Yes, it is in stock right now.", []),
    ("Order TW10001 please", "Order TW10001 shipped.", []),
    # a number is not an identifier; lowercase words are not
    ("price?", "It costs 1999 and ships in 2 days; see page abc1234.", []),
])
def test_what_is_an_ungrounded_identifier(prompt, answer, bad):
    assert ig.ungrounded(prompt, answer) == bad


def test_the_score_is_a_proportion_with_its_wilson_bounds():
    rows = [{"item_id": "a", "prompt": "q", "student_answer": "fine. (case C00001)"},
            {"item_id": "b", "prompt": "q", "answer": "fine."},
            {"prompt_id": "c", "prompt": "TW10001?", "answer": "TW10001 shipped."}]
    s = ig.score(rows)
    assert (s["value"], s["count"], s["identifier_n"]) == (2 / 3, "2/3", 3)
    assert s["ungrounded"] == [{"item_id": "a", "identifiers": ["C00001"]}]
    assert 0 < s["wilson95_ci_low"] < 2 / 3 < s["wilson95_ci_high"] < 1


@pytest.mark.parametrize("k,low", [(120, 0.969), (119, 0.9543), (118, 0.9413), (0, 0.0)])
def test_the_bounds_the_gate_decides_on(k, low):
    assert round(ig.wilson(k, 120)[0], 4) == low


def test_the_demo_preset_gates_at_a_threshold_a_120_item_set_can_pass():
    """0.99 was the first value written: 120/120's lower bound is 0.969, so it could never pass."""
    src = (REPO / "deploy/console/lambda_function.py").read_text()
    assert '"identifier_grounding": 0.95' in src
    t = 0.95
    assert ig.wilson(120, 120)[0] >= t and ig.wilson(119, 120)[0] >= t > ig.wilson(118, 120)[0]


def test_the_cli_prints_one_json_object(tmp_path, capsys):
    f = tmp_path / "a.jsonl"
    f.write_text(json.dumps({"item_id": "a", "prompt": "q", "answer": "x (case C00001)"}) + "\n", encoding="utf-8")
    assert ig.main([str(f)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["metric"] == "identifier_grounding" and out["value"] == 0.0 and out["scorer"] == ig.SCORER


def test_the_eval_prompt_runs_this_scorer_verbatim_and_gates_it():
    text = json.loads((REPO / "agents/eval/harness.json").read_text())["systemPrompt"][0]["text"]
    i = text.find("CANONICAL deterministic scorer at s3://<bucket>/code/eval/identifier_grounding.py")
    assert i > 0, "the eval prompt no longer names the canonical scorer"
    clause = text[i - 200:i + 1100]
    # And the score step itself names it, where the agent is when it scores.
    score = text[text.find('- "score":'):]
    assert "s3://<bucket>/code/eval/identifier_grounding.py" in score[:600]
    for must in ("VERBATIM", "identifier_scorer_sha256", "do NOT author your own", "identifier_n",
                 "Wilson 95%", "params.gates names identifier_grounding", "except vision"):
        assert must in clause, f"the clause lost {must!r}"


def test_the_scorer_is_mirrored_where_the_prompt_reads_it():
    """03_storage.py mirrors every file in pipeline/eval/ to code/eval/; a scorer outside it is one
    the eval agent cannot download."""
    assert (REPO / "pipeline/eval/identifier_grounding.py").is_file()
    assert 'EVAL_PREFIX = "code/eval/"' in (REPO / "deploy/03_storage.py").read_text()
