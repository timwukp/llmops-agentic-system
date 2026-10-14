"""pipeline/lakehouse/generate.py: deterministic, schema-true, and every planted defect exactly
where expected_defects.json says it is.

The Silver/Gold rules (PR-07) will be tested against this generator's output, so a plant that is
off by one -- a "clean" conversation that happens to carry a phone number, a near-duplicate that
is an exact duplicate -- would make a correct rule look wrong or a wrong rule look right. Each
defect is therefore re-detected here by an independent oracle that does not read
expected_defects.json, and the two must agree exactly, in both directions.

Rows are checked against deploy/10_lakehouse.py's TABLES -- the deployed schemas -- rather than
against a copy of them, so a column added on either side fails here.
"""
from __future__ import annotations

import collections
import datetime
import difflib
import hashlib
import importlib.util
import json
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen = _load("lakehouse_generate", "pipeline/lakehouse/generate.py")
lh = _load("lakehouse_deploy_for_generate", "deploy/10_lakehouse.py")


@pytest.fixture(scope="module", params=["small", "demo"])
def ds(request):
    return gen.generate(seed=7, scale=request.param)


def _turns(ds):
    by = collections.defaultdict(dict)
    for e in ds["tables"]["conversation_events"]:
        by[e["conversation_id"]][e["turn_idx"]] = e
    return by


def _ids(ds, defect):
    return {i["conversation_id"] for i in ds["expected_defects"][defect]["items"]}


# ── determinism ──────────────────────────────────────────────────────────────
def test_the_same_seed_writes_byte_identical_files(tmp_path):
    a = gen.write(gen.generate(7, "small"), tmp_path / "a")
    b = gen.write(gen.generate(7, "small"), tmp_path / "b")
    assert a["sha256"] == b["sha256"] and a["dataset_sha256"] == b["dataset_sha256"]
    for name, digest in a["sha256"].items():
        assert hashlib.sha256((tmp_path / "a" / name).read_bytes()).hexdigest() == digest


def test_a_different_seed_writes_a_different_dataset(tmp_path):
    a = gen.write(gen.generate(7, "small"), tmp_path / "a")
    b = gen.write(gen.generate(8, "small"), tmp_path / "b")
    assert a["dataset_sha256"] != b["dataset_sha256"]


# ── schema: the deployed tables, not a copy ──────────────────────────────────
_PY = {"string": str, "int": int, "long": int}


def _ts_ok(v):
    return isinstance(v, str) and datetime.datetime.strptime(v, "%Y-%m-%dT%H:%M:%S.%fZ") is not None


@pytest.mark.parametrize("table", ["conversation_events", "crm_cdc", "erp_cdc", "web_events"])
def test_every_row_matches_the_deployed_bronze_schema(ds, table):
    spec = next(t for t in lh.TABLES if t["namespace"] == "bronze" and t["name"] == table)
    cols = [f["name"] for f in spec["fields"]]
    for row in ds["tables"][table]:
        assert sorted(row) == sorted(cols), (table, sorted(set(row) ^ set(cols)))
        for f in spec["fields"]:
            v = row[f["name"]]
            if v is None:
                assert not f["required"], (table, f["name"])
            elif f["type"] == "timestamptz":
                assert _ts_ok(v), (table, f["name"], v)
            else:
                assert isinstance(v, _PY[f["type"]]) and not isinstance(v, bool), (table, f["name"], v)


def test_every_bronze_table_the_generator_writes_is_deployed():
    deployed = {t["name"] for t in lh.TABLES if t["namespace"] == "bronze"}
    assert set(gen.generate(7, "small")["tables"]) <= deployed


# ── each planted defect, re-detected independently ───────────────────────────
_PII_ORACLE = {
    "TW_ID": re.compile(r"(?<![A-Za-z0-9])[A-Z][12][0-9]{8}(?![0-9])"),
    "TW_MOBILE": re.compile(r"09[0-9]{2}-[0-9]{3}-[0-9]{3}"),
    "EMAIL": re.compile(r"[\w.]+@[\w.]+\.[a-z]{2,}"),
    "CARD": re.compile(r"(?:[0-9]{4}-){3}[0-9]{4}"),
}


def test_pii_appears_exactly_in_the_planted_turns(ds):
    turns, planted = _turns(ds), ds["expected_defects"]["pii"]["items"]
    by_conv = {p["conversation_id"]: p for p in planted}
    for conv, ts in turns.items():
        text = " ".join(t["content"] for t in ts.values())
        found = {k for k, rx in _PII_ORACLE.items() if rx.search(text)}
        p = by_conv.get(conv)
        if p is None:
            assert not found, f"PII in a conversation nothing planted it in: {conv} {found}"
        else:
            assert p["value"] in ts[p["turn_idx"]]["content"]
            if p["type"] in _PII_ORACLE:
                assert found == {p["type"]}, (conv, found, p["type"])
    assert {p["type"] for p in planted} == set(gen.PII_KINDS)


def test_planted_identifiers_are_valid_so_a_checksum_rule_must_accept_them(ds):
    for p in ds["expected_defects"]["pii"]["items"]:
        if p["type"] == "TW_ID":
            assert gen.tw_id_checksum_ok(p["value"])
        if p["type"] == "CARD":
            assert gen.luhn_ok(p["value"].replace("-", ""))


def test_think_traces_appear_exactly_where_planted(ds):
    hits = {e["conversation_id"] for e in ds["tables"]["conversation_events"] if "<think>" in e["content"]}
    assert hits == _ids(ds, "leaked_think")
    assert all(e["role"] == "agent" for e in ds["tables"]["conversation_events"] if "<think>" in e["content"])


def test_toxic_turns_appear_exactly_where_planted(ds):
    markers = tuple(gen._TOXIC.values())
    hits = {e["conversation_id"] for e in ds["tables"]["conversation_events"]
            if e["content"].startswith(markers)}
    assert hits == _ids(ds, "toxic")


def test_eval_prompts_reach_training_conversations_only_where_planted(ds):
    prompts = {p["prompt"] for p in ds["eval_prompts"]}
    hits = {e["conversation_id"] for e in ds["tables"]["conversation_events"] if e["content"] in prompts}
    assert hits == _ids(ds, "eval_contamination")


def test_out_of_order_ingestion_happens_exactly_where_planted(ds):
    late = set()
    for conv, ts in _turns(ds).items():
        by_turn = [ts[i]["ingest_ts"] for i in sorted(ts)]
        if by_turn != sorted(by_turn):
            late.add(conv)
    assert late == _ids(ds, "out_of_order")


def test_near_duplicates_differ_from_their_source_by_one_small_edit(ds):
    turns = _turns(ds)
    texts = {c: "\n".join(t[i]["content"] for i in sorted(t)) for c, t in turns.items()}
    for item in ds["expected_defects"]["near_duplicate"]["items"]:
        a, b = texts[item["conversation_id"]], texts[item["copy_of"]]
        assert a != b, "a near-duplicate must not be an exact duplicate"
        assert difflib.SequenceMatcher(None, a, b).ratio() > 0.95, (a, b)
    exact = collections.Counter(texts.values())
    assert max(exact.values()) == 1, "no two conversations may be exact copies"


def test_resent_events_are_exact_duplicates_and_the_only_ones(ds):
    counts = collections.Counter(e["event_id"] for e in ds["tables"]["conversation_events"])
    dup = sorted(k for k, n in counts.items() if n > 1)
    assert dup == ds["expected_defects"]["duplicate_resend"]["items"]
    assert all(counts[k] == 2 for k in dup)
    rows = collections.defaultdict(list)
    for e in ds["tables"]["conversation_events"]:
        rows[e["event_id"]].append(json.dumps(e, sort_keys=True))
    assert all(len(set(rows[k])) == 1 for k in dup), "a resend is byte-identical"


def test_schema_drift_starts_at_its_conversation_and_never_before(ds):
    drift = [e for e in ds["tables"]["conversation_events"] if "channel_detail" in e["raw_meta"]]
    # Counted as distinct events: a resent drifted event is one drifted event delivered twice.
    assert len({e["event_id"] for e in drift}) == ds["expected_defects"]["schema_drift"]["count"] > 0
    first = min(e["ingest_ts"] for e in drift)
    assert all("channel_detail" in e["raw_meta"] for e in ds["tables"]["conversation_events"]
               if e["ingest_ts"] > first), "once the field appears it stays"


def test_conversation_defects_are_disjoint(ds):
    seen = collections.Counter(c for d in gen.CONVERSATION_DEFECTS for c in _ids(ds, d))
    assert max(seen.values()) == 1


def test_planted_counts_are_the_configured_counts(ds):
    want = gen.DEFECTS[ds["scale"]]
    for d in gen.CONVERSATION_DEFECTS + ("duplicate_resend",):
        assert ds["expected_defects"][d]["count"] == want[d], d


# ── CDC envelopes and referential integrity ──────────────────────────────────
@pytest.mark.parametrize("table", ["crm_cdc", "erp_cdc"])
def test_cdc_envelopes_follow_debezium_shape(ds, table):
    last = {}
    for r in ds["tables"][table]:
        src = json.loads(r["source"])
        assert (r["before"] is None) == (r["op"] == "c") and (r["after"] is None) == (r["op"] == "d")
        row = json.loads(r["after"] or r["before"])
        key = (src["table"], row.get("customer_id") if src["table"] == "customers" else
               row.get("ticket_id") or row.get("order_id") or (row.get("sku"), row.get("store")))
        assert r["ts_ms"] >= last.get(key, 0), ("ts_ms went backwards", key)
        last[key] = r["ts_ms"]


def test_tickets_reference_real_customers_and_conversations(ds):
    customers = {json.loads(r["after"])["customer_id"] for r in ds["tables"]["crm_cdc"]
                 if r["op"] == "c" and json.loads(r["source"])["table"] == "customers"}
    convs = {e["conversation_id"] for e in ds["tables"]["conversation_events"]}
    tickets = [json.loads(r["after"]) for r in ds["tables"]["crm_cdc"]
               if r["op"] == "c" and json.loads(r["source"])["table"] == "tickets"]
    assert tickets and all(t["customer_id"] in customers and t["conversation_id"] in convs for t in tickets)


# ── languages ────────────────────────────────────────────────────────────────
#: Simplified-only forms of characters the templates use (訂單貨運優庫們這麼); 存 and 惠 are the
#: same in both scripts, so they cannot tell the two apart.
_SIMPLIFIED = set("订单货运优库们这么")


def test_both_languages_appear_and_chinese_is_traditional(ds):
    langs = collections.Counter(e["lang"] for e in ds["tables"]["conversation_events"])
    assert set(langs) == {"zh-TW", "en"} and 0.4 < langs["zh-TW"] / sum(langs.values()) < 0.8
    zh = "".join(e["content"] for e in ds["tables"]["conversation_events"] if e["lang"] == "zh-TW")
    assert re.search(r"[一-鿿]", zh) and not (_SIMPLIFIED & set(zh))


def test_the_cli_writes_a_manifest_whose_hashes_match_the_files(tmp_path):
    assert gen.main(["--seed", "3", "--scale", "small", "--out", str(tmp_path)]) == 0
    m = json.loads((tmp_path / "manifest.json").read_text())
    for name, digest in m["sha256"].items():
        assert hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() == digest
    assert m["counts"]["conversation_events"] == sum(1 for _ in open(tmp_path / "bronze_conversation_events.jsonl"))
