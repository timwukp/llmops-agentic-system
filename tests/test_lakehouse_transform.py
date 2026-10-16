"""pipeline/lakehouse/transform.py against the generator's planted defects: every rule catches
exactly what was planted, nothing else, at both scales and for several seeds.

This is the contract the research report set (§2: "each defect has a Silver/Gold rule that must
catch it and a test that proves the rule catches it"). Agreement is checked in both directions --
a rule that also catches clean rows fails as surely as one that misses a plant. Name detection is
injected (Bedrock Guardrails in the Glue job, per spike S4); here a dictionary double stands in,
so these tests prove the plumbing and the structured layer, while S4's evidence carries the
Guardrails recall.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen = _load("lh_gen_for_transform", "pipeline/lakehouse/generate.py")
tf = _load("lh_transform", "pipeline/lakehouse/transform.py")
lh = _load("lh_deploy_for_transform", "deploy/10_lakehouse.py")

CASES = [(seed, scale) for seed in (7, 8, 11) for scale in ("small", "demo")]


def _run(seed, scale):
    ds = gen.generate(seed, scale)
    names = {i["value"] for i in ds["expected_defects"]["pii"]["items"] if i["type"] == "NAME"}
    rows, report = tf.bronze_to_silver(
        ds["tables"]["conversation_events"], [p["prompt"] for p in ds["eval_prompts"]],
        name_detector=lambda text: [n for n in names if n in text],
        dq_run_id="dq-test", updated_ts="2026-10-14T00:00:00.000Z")
    return ds, rows, report


@pytest.fixture(scope="module", params=CASES, ids=[f"seed{s}-{c}" for s, c in CASES])
def run(request):
    return _run(*request.param)


def _planted(ds, defect):
    return {i["conversation_id"] for i in ds["expected_defects"][defect]["items"]}


def _caught(report, rule):
    return set(report["rules"][rule]["items"])


@pytest.mark.parametrize("rule,defect", [
    ("order_by_turn", "out_of_order"), ("strip_think", "leaked_think"), ("drop_toxic", "toxic"),
    ("redact_pii", "pii"), ("decontaminate", "eval_contamination"),
])
def test_each_conversation_rule_catches_exactly_its_plant(run, rule, defect):
    ds, _, report = run
    assert _caught(report, rule) == _planted(ds, defect), rule


def test_resends_are_deduplicated_exactly(run):
    ds, _, report = run
    assert sorted(_caught(report, "dedup_event_id")) == ds["expected_defects"]["duplicate_resend"]["items"]


def test_schema_drift_is_tolerated_and_counted(run):
    ds, _, report = run
    assert report["rules"]["tolerate_drift"]["count"] == ds["expected_defects"]["schema_drift"]["count"]


def test_each_near_duplicate_pair_loses_exactly_one_side(run):
    ds, _, report = run
    caught = _caught(report, "near_dup")
    items = ds["expected_defects"]["near_duplicate"]["items"]
    assert len(caught) == len(items)
    for it in items:
        assert (it["conversation_id"] in caught) != (it["copy_of"] in caught), it


def test_silver_pairs_are_clean_unique_and_schema_true(run):
    _, rows, _ = run
    spec = next(t for t in lh.TABLES if t["namespace"] == "silver" and t["name"] == "instruction_pairs")
    cols = sorted(f["name"] for f in spec["fields"])
    assert rows and len({r["pair_id"] for r in rows}) == len(rows)
    for r in rows:
        assert sorted(r) == cols
        for f in spec["fields"]:
            if f["required"]:
                assert r[f["name"]] not in (None, ""), f["name"]
        text = r["instruction"] + r["output"]
        assert "<think>" not in text
        assert not any(rx.search(text) and (check is None or check(rx.search(text).group(0)))
                       for _, rx, check in tf.STRUCTURED), text
        assert not any(w in r["instruction"].lower() for w in tf.TOXIC_LEXICON)


def test_planted_names_never_reach_silver(run):
    ds, rows, _ = run
    names = {i["value"] for i in ds["expected_defects"]["pii"]["items"] if i["type"] == "NAME"}
    assert not [r for r in rows for n in names if n in r["instruction"] + r["output"]]


def test_eval_prompts_never_reach_silver(run):
    ds, rows, _ = run
    prompts = {p["prompt"] for p in ds["eval_prompts"]}
    assert not [r for r in rows if r["instruction"] in prompts]


def test_pair_counts_add_up(run):
    ds, rows, report = run
    dropped = sum(report["rules"][r]["count"] for r in ("drop_toxic", "decontaminate", "near_dup"))
    assert report["pairs"] == len(rows) == report["conversations"] - dropped


# ── Silver → Gold ────────────────────────────────────────────────────────────
def test_gold_split_is_deterministic_and_disjoint(run):
    _, rows, _ = run
    train, ev = tf.silver_to_gold(rows)
    train2, ev2 = tf.silver_to_gold(list(reversed(rows)))
    assert {r["pair_id"] for r in train} == {r["pair_id"] for r in train2}
    assert not {r["pair_id"] for r in train} & {r["pair_id"] for r in ev}
    excluded = [r for r in rows if tf.carries_redaction(r)]
    assert len(train) + len(ev) + len(excluded) == len(rows) and ev, "an empty eval split would gate on nothing"
    assert all(r["split"] == "train" for r in train) and all(r["split"] == "eval" for r in ev)


def test_gold_rows_match_the_deployed_gold_schema():
    _, rows, _ = _run(7, "small")
    train, _ = tf.silver_to_gold(rows)
    spec = next(t for t in lh.TABLES if t["namespace"] == "gold" and t["name"] == "qlora_train")
    assert all(sorted(r) == sorted(f["name"] for f in spec["fields"]) for r in train)


def test_messages_mapping_matches_the_trainer_contract():
    m = tf.to_messages({"instruction": "Q", "input": "", "output": "A"})
    assert m == {"messages": [{"role": "user", "content": "Q"}, {"role": "assistant", "content": "A"}]}
    assert tf.to_messages({"instruction": "Q", "input": "ctx", "output": "A"})["messages"][0]["content"] == "Q\n\nctx"


# ── the PII layer in isolation ───────────────────────────────────────────────
@pytest.mark.parametrize("text,placeholder", [
    ("my ID number is A123456789, ", "[TW_ID]"), ("我的手機是0912-345-678，", "[PHONE]"),
    ("my email is kevin42@example.com, ", "[EMAIL]"), ("card number 4111-1111-1111-1111, ", "[CARD]"),
    ("公司統一編號04595257", "[UBN]"),
])
def test_structured_identifiers_are_redacted(text, placeholder):
    out, found = tf.redact_structured(text)
    assert placeholder in out and placeholder in found


@pytest.mark.parametrize("text", [
    "訂單 TW10084 已出貨", "card number 4111-1111-1111-1112", "ID A123456788", "共 12345678 件",
])
def test_lookalikes_that_fail_their_checksum_are_left_alone(text):
    assert tf.redact_structured(text) == (text, [])


def test_dqdl_rules_name_the_gates_the_report_specifies():
    assert 'Uniqueness "pair_id" = 1.0' in tf.SILVER_DQDL and "<think>" in tf.SILVER_DQDL
    assert 'ColumnValues "split" in ["train", "eval"]' in tf.GOLD_DQDL
    json.dumps(tf.SILVER_DQDL)  # a plain string, embeddable in a Glue job argument


def test_compare_to_plants_agrees_on_a_correct_run_and_names_a_wrong_one(run):
    ds, _, report = run
    result = tf.compare_to_plants(report["rules"], ds["expected_defects"])
    assert set(result) == set(tf.RULE_FOR_DEFECT) and all(ok for ok, _ in result.values()), result
    broken = json.loads(json.dumps(report["rules"]))
    broken["drop_toxic"]["items"] = broken["drop_toxic"]["items"][1:]
    broken["drop_toxic"]["count"] -= 1
    assert tf.compare_to_plants(broken, ds["expected_defects"])["drop_toxic"][0] is False


# ── organisation-owned entities are not names ────────────────────────────────
REF = json.loads((REPO / "pipeline/lakehouse/reference/org_entities.json").read_text())


def test_every_store_and_product_the_generator_writes_is_in_the_reference_data():
    keep = tf.org_entities(REF)
    for zh, en in gen._STORES + gen._PRODUCTS:
        assert zh in keep and en in keep, (zh, en)


def test_a_store_name_read_as_a_person_is_not_redacted():
    """The live demo-scale false positives: Guardrails returned district-named stores as NAME."""
    keep = tf.org_entities(REF)
    over_eager = lambda text: ["前鎮", "Taichung Xitun", "陳志明"]  # noqa: E731
    out, found = tf.redact("高雄前鎮店還有運動鞋的庫存嗎？我是陳志明", over_eager, keep)
    assert out == "高雄前鎮店還有運動鞋的庫存嗎？我是[NAME]" and found == ["[NAME]"]
    out, found = tf.redact("Does Taichung Xitun still have the power bank in stock?", over_eager, keep)
    assert found == [] and "Taichung Xitun" in out


def test_the_keep_list_protects_only_where_the_entity_is_actually_present():
    keep = tf.org_entities(REF)
    out, found = tf.redact("我是前鎮，訂單出問題", lambda t: ["前鎮"], keep)  # no store in this text
    assert out == "我是[NAME]，訂單出問題" and found == ["[NAME]"]



# ── redacted text is still training text (PR-11) ────────────────────────────
def test_gold_promotes_no_pair_that_carries_a_redaction_token(run):
    """Silver keeps every redacted pair (the audited record); Gold, where training data is chosen,
    takes none. Live, 27 such rows taught a 1.7B student to coin `[ORDERID]` in held-out answers."""
    ds, rows, report = run
    train, ev = tf.silver_to_gold(rows)
    redacted = {r["pair_id"] for r in rows if tf.carries_redaction(r)}
    assert redacted, "the fixture must plant PII for this to test anything"
    assert not redacted & {r["pair_id"] for r in train + ev}
    assert all(not tf.carries_redaction(r) for r in train + ev)
    assert len(redacted) <= report["rules"]["redact_pii"]["count"]


def test_the_gold_dq_rule_names_every_token_the_redaction_writes():
    for token in tf.redaction_tokens():
        assert f"instruction LIKE '%{token}%'" in tf.GOLD_DQDL and f"output LIKE '%{token}%'" in tf.GOLD_DQDL
    assert tf.GOLD_DQDL.startswith("Rules = [") and tf.GOLD_DQDL.endswith(" = 0 ]")
