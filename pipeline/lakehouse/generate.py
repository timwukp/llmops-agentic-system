#!/usr/bin/env python3
"""Seeded generator for the lakehouse sources (Phase 1 PR-06; research report §2 and §4.1).

One fictional retailer ("SEA retail customer-service copilot"), four Bronze sources in the exact
shapes of deploy/10_lakehouse.py's tables:

  conversation_events   customer-service chat/email turns, Traditional Chinese and English
  crm_cdc               Debezium-style change events for customers and tickets
  erp_cdc               Debezium-style change events for orders and inventory
  web_events            page views, searches, carts and checkouts

and the defects the Silver/Gold rules (PR-07) must catch, PLANTED with exact counts and ids in
`expected_defects.json`:

  pii                   a name, phone, email, Taiwanese national ID or card number in a customer turn
  toxic                 an abusive customer turn
  leaked_think          an agent turn carrying a <think>…</think> trace
  out_of_order          a conversation whose turns were ingested out of turn order
  near_duplicate        a conversation copied from another with one word changed
  eval_contamination    a customer question copied verbatim from the eval prompt set
  duplicate_resend      an event delivered twice with the same event_id
  schema_drift          events whose raw_meta gains a field mid-stream

Deterministic by construction: one `random.Random(seed)`, ids from uuid5, timestamps from a fixed
base. The same seed and scale produce byte-identical files and the same sha256 on any machine,
which is what lets a test assert that a rule caught exactly the planted defects and nothing else.

Conversations are template-built, not teacher-written (the report's §4.1 suggests the Bedrock
teacher): templates are what make every defect countable. A paraphrase pass through the teacher,
if wanted, belongs in the pipeline (a data-prep task, PR-09), not in this offline generator.

    python3 pipeline/lakehouse/generate.py --seed 7 --scale demo --out /tmp/lakehouse-demo
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import pathlib
import random
import re
import sys
import uuid

GENERATOR_VERSION = "1"
NS = uuid.UUID("6f1d3c3e-7c4b-4e62-9a43-1b2f0c5d8e71")  # fixed namespace for uuid5 ids
BASE = _dt.datetime(2026, 9, 1, tzinfo=_dt.timezone.utc)

SCALES = {
    "small": {"customers": 40, "skus": 20, "orders": 120, "conversations": 80,
              "web_events": 300, "eval_prompts": 20},
    "demo": {"customers": 200, "skus": 50, "orders": 600, "conversations": 300,
             "web_events": 2000, "eval_prompts": 40},
}
#: Planted defects per scale. Conversation-level defects land in DISJOINT conversations, so a
#: rule's catch can be compared with the plant one to one.
DEFECTS = {
    "small": {"pii": 10, "toxic": 3, "leaked_think": 4, "out_of_order": 3, "near_duplicate": 3,
              "eval_contamination": 3, "duplicate_resend": 5, "schema_drift_from": 60},
    "demo": {"pii": 30, "toxic": 6, "leaked_think": 10, "out_of_order": 8, "near_duplicate": 6,
             "eval_contamination": 5, "duplicate_resend": 12, "schema_drift_from": 240},
}
CONVERSATION_DEFECTS = ("pii", "toxic", "leaked_think", "out_of_order", "near_duplicate",
                        "eval_contamination")
PII_KINDS = ("NAME", "TW_MOBILE", "EMAIL", "TW_ID", "CARD")
INTENTS = ("order_status", "return_request", "stock_check", "promo_question", "complaint")
ZH_SHARE = 0.6

# ── fictional reference data ─────────────────────────────────────────────────
_SURNAMES = "陳林黃張李王吳劉蔡楊許鄭謝洪郭邱曾廖賴徐"
_GIVEN = ["志明", "淑芬", "家豪", "雅婷", "冠宇", "怡君", "俊傑", "佩珊", "建宏", "欣怡", "宗翰", "美玲"]
_EN_FIRST = ["Kevin", "Grace", "Jason", "Emily", "Daniel", "Vivian", "Eric", "Joanne", "Allen", "Tina"]
_EN_LAST = ["Chen", "Lin", "Huang", "Chang", "Lee", "Wang", "Wu", "Liu", "Tsai", "Yang"]
_PRODUCTS = [("無線耳機", "wireless earbuds"), ("保溫瓶", "insulated bottle"), ("行動電源", "power bank"),
             ("登山背包", "hiking backpack"), ("咖啡豆", "coffee beans"), ("運動鞋", "running shoes"),
             ("電動牙刷", "electric toothbrush"), ("藍牙喇叭", "bluetooth speaker")]
_STORES = [("台北信義店", "Taipei Xinyi"), ("台中西屯店", "Taichung Xitun"), ("高雄前鎮店", "Kaohsiung Qianzhen")]
_ID_LETTER = {c: v for c, v in zip("ABCDEFGHJKLMNPQRSTUVXYWZIO", range(10, 36))}

#: (customer question, agent answer) per intent and language. {o} order, {p} product, {s} store.
_TEMPLATES = {
    ("order_status", "zh-TW"): [("請問訂單 {o} 目前寄到哪裡了？", "您好，訂單 {o} 已於昨日出貨，預計兩天內送達。"),
                                ("我的訂單 {o} 什麼時候會到？", "訂單 {o} 目前在物流中心，明天會配送。")],
    ("order_status", "en"): [("Where is my order {o} right now?", "Your order {o} shipped yesterday and should arrive within two days."),
                             ("When will order {o} arrive?", "Order {o} is at the warehouse and goes out for delivery tomorrow.")],
    ("return_request", "zh-TW"): [("我想退貨，訂單 {o} 的{p}有瑕疵。", "很抱歉造成不便，已為訂單 {o} 建立退貨單，將派員收件。"),
                                  ("{p}尺寸不合，訂單 {o} 可以退嗎？", "可以的，訂單 {o} 在七天鑑賞期內，已為您申請退貨。")],
    ("return_request", "en"): [("I want to return the {p} from order {o}, it is defective.", "Sorry about that. A return for order {o} is created and a courier will collect it."),
                               ("The {p} in order {o} does not fit, can I return it?", "Yes, order {o} is within the 7-day window; the return is filed.")],
    ("stock_check", "zh-TW"): [("{s}還有{p}的庫存嗎？", "{s}目前{p}尚有庫存，歡迎到店選購。"),
                               ("{p}什麼時候會補貨？", "{p}預計下週補貨，到貨後會通知您。")],
    ("stock_check", "en"): [("Does {s} still have the {p} in stock?", "{s} has the {p} in stock right now."),
                            ("When will the {p} be restocked?", "The {p} is expected back next week; we will notify you.")],
    ("promo_question", "zh-TW"): [("{p}的週年慶折扣可以和會員點數一起用嗎？", "可以，週年慶折扣與會員點數可以同時使用。"),
                                  ("請問{p}現在有優惠嗎？", "{p}本週享九折優惠，結帳時自動折抵。")],
    ("promo_question", "en"): [("Can the anniversary discount on the {p} be combined with member points?", "Yes, the anniversary discount and member points can be combined."),
                               ("Is there a promotion on the {p} now?", "The {p} is 10% off this week, applied at checkout.")],
    ("complaint", "zh-TW"): [("訂單 {o} 的配送員態度很差，我要客訴。", "非常抱歉，我們已記錄訂單 {o} 的客訴，主管會在一天內回覆您。")],
    ("complaint", "en"): [("The courier for order {o} was rude, I want to complain.", "We are very sorry. The complaint on order {o} is logged; a supervisor will reply within a day.")],
}
_FOLLOW = {"zh-TW": ("好的，謝謝。", "不客氣，祝您有美好的一天！"), "en": ("Great, thanks.", "You're welcome, have a nice day!")}
#: Eval prompts use their own phrasing so a clean conversation can never equal one by accident.
_EVAL = {"zh-TW": ["想確認一下{p}的保固期限是多久？", "{p}可以用信用卡分期付款嗎？", "如果{p}在運送途中損壞要怎麼處理？"],
         "en": ["How long is the warranty on the {p}?", "Can I pay for the {p} in installments?",
                "What happens if the {p} is damaged in transit?"]}
_TOXIC = {"zh-TW": "你們客服真的是一群白痴，", "en": "you people are useless idiots, "}
_PII_LEAD = {"zh-TW": {"NAME": "我是{v}，", "TW_MOBILE": "我的手機是{v}，", "EMAIL": "電子郵件{v}，",
                       "TW_ID": "身分證字號{v}，", "CARD": "信用卡號{v}，"},
             "en": {"NAME": "this is {v}, ", "TW_MOBILE": "my mobile is {v}, ", "EMAIL": "my email is {v}, ",
                    "TW_ID": "my ID number is {v}, ", "CARD": "card number {v}, "}}
_THINK = "<think>The customer seems upset; check the order system before answering.</think>"
_NEAR_DUP_SWAP = (("請問", "請教"), ("Where", "Where exactly"), ("可以", "能"), ("When", "So when"))


def tw_id_checksum_ok(s: str) -> bool:
    if not re.fullmatch(r"[A-Z][12][0-9]{8}", s):
        return False
    n = _ID_LETTER[s[0]]
    digits = [n // 10, n % 10] + [int(c) for c in s[1:]]
    return sum(d * w for d, w in zip(digits, [1, 9, 8, 7, 6, 5, 4, 3, 2, 1, 1])) % 10 == 0


def luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for c in reversed(digits):
        d = int(c)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total, alt = total + d, not alt
    return total % 10 == 0


def _pii_value(kind: str, lang: str, rng: random.Random) -> str:
    if kind == "NAME":
        return (rng.choice(_SURNAMES) + rng.choice(_GIVEN) if lang == "zh-TW"
                else f"{rng.choice(_EN_FIRST)} {rng.choice(_EN_LAST)}")
    if kind == "TW_MOBILE":
        n = "".join(rng.choice("0123456789") for _ in range(8))
        return f"09{n[:2]}-{n[2:5]}-{n[5:]}"
    if kind == "EMAIL":
        return f"{rng.choice(['kevin', 'grace.lin', 'jason_wu', 'emily88'])}{rng.randint(1, 999)}@example.com"
    if kind == "TW_ID":
        while True:
            s = rng.choice("ABCDEFGHJKLMNPQRSTUVXYWZ") + rng.choice("12") + "".join(
                rng.choice("0123456789") for _ in range(8))
            if tw_id_checksum_ok(s):
                return s
    if kind == "CARD":  # written with separators, so never a 12-digit run
        while True:
            d = "4" + "".join(rng.choice("0123456789") for _ in range(15))
            if luhn_ok(d):
                return "-".join(d[i:i + 4] for i in range(0, 16, 4))
    raise ValueError(kind)


# ── generation ───────────────────────────────────────────────────────────────
class _Ids:
    def __init__(self, seed):
        self.seed = seed

    def __call__(self, kind: str, *parts) -> str:
        return str(uuid.uuid5(NS, f"{self.seed}:{kind}:" + ":".join(map(str, parts))))


def _ts(seconds: float) -> str:
    return (BASE + _dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _ms(seconds: float) -> int:
    return int((BASE + _dt.timedelta(seconds=seconds)).timestamp() * 1000)


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _cdc(op, before, after, seconds, db, table):
    return {"op": op, "before": _j(before) if before is not None else None,
            "after": _j(after) if after is not None else None, "ts_ms": _ms(seconds),
            "source": _j({"db": db, "table": table}), "ingest_ts": _ts(seconds + 2)}


def generate(seed: int = 7, scale: str = "demo") -> dict:
    """Return {"tables": {name: [rows]}, "eval_prompts": [...], "expected_defects": {...}}."""
    size, plant = SCALES[scale], DEFECTS[scale]
    rng, ids = random.Random(seed), _Ids(seed)
    crm, erp, web = [], [], []

    customers = []
    for i in range(size["customers"]):
        lang = "zh-TW" if rng.random() < ZH_SHARE else "en"
        c = {"customer_id": ids("customer", i), "name": _pii_value("NAME", lang, rng),
             "email": f"customer{i:04d}@example.com", "tier": rng.choice(["basic", "silver", "gold"]),
             "lang": lang}
        customers.append(c)
        crm.append(_cdc("c", None, c, i * 60, "crm", "customers"))
    skus = [{"sku": f"SKU-{i:04d}", "product": i % len(_PRODUCTS)} for i in range(size["skus"])]
    for i, s in enumerate(skus):
        for st in range(len(_STORES)):
            inv = {"sku": s["sku"], "store": st, "on_hand": rng.randint(0, 80)}
            erp.append(_cdc("c", None, inv, i * 30 + st, "erp", "inventory"))
    orders = []
    for i in range(size["orders"]):
        cust, sku = rng.choice(customers), rng.choice(skus)
        o = {"order_id": f"TW{10000 + i}", "customer_id": cust["customer_id"], "sku": sku["sku"],
             "qty": rng.randint(1, 3), "amount": rng.randint(199, 4999), "status": "created"}
        orders.append(o)
        t = 86400 + i * 300
        erp.append(_cdc("c", None, o, t, "erp", "orders"))
        shipped = {**o, "status": "shipped"}
        erp.append(_cdc("u", o, shipped, t + 3600, "erp", "orders"))
    for c in rng.sample(customers, k=max(1, size["customers"] // 10)):  # tier upgrades
        crm.append(_cdc("u", c, {**c, "tier": "gold"}, 7 * 86400, "crm", "customers"))
    for c in rng.sample(customers, k=max(1, size["customers"] // 40)):  # account deletions
        crm.append(_cdc("d", c, None, 8 * 86400, "crm", "customers"))

    eval_prompts = []
    for i in range(size["eval_prompts"]):
        lang = "zh-TW" if i % 5 < 3 else "en"
        p = _PRODUCTS[i % len(_PRODUCTS)][0 if lang == "zh-TW" else 1]
        eval_prompts.append({"prompt_id": ids("eval", i), "lang": lang,
                             "prompt": _EVAL[lang][i % len(_EVAL[lang])].format(p=p) + f"（#{i}）"
                             if lang == "zh-TW" else _EVAL[lang][i % len(_EVAL[lang])].format(p=p) + f" (#{i})"})

    n_conv = size["conversations"]
    pool = list(range(n_conv))
    rng.shuffle(pool)
    assigned, cursor = {}, 0
    for d in CONVERSATION_DEFECTS:
        assigned[d] = sorted(pool[cursor:cursor + plant[d]])
        cursor += plant[d]
    near_dup_sources = sorted(pool[cursor:cursor + plant["near_duplicate"]])
    by_conv = {i: d for d, idx in assigned.items() for i in idx}

    events, expected = [], {d: [] for d in CONVERSATION_DEFECTS}
    convs = {}
    for i in range(n_conv):
        if by_conv.get(i) == "near_duplicate":
            continue  # built from its source below
        convs[i] = _conversation(i, rng, ids, customers, orders, skus)
    for k, i in enumerate(assigned["near_duplicate"]):
        src = near_dup_sources[k]
        if src not in convs:
            convs[src] = _conversation(src, rng, ids, customers, orders, skus)
        convs[i] = _near_duplicate(convs[src], i, ids)
        expected["near_duplicate"].append({"conversation_id": convs[i]["conversation_id"],
                                           "copy_of": convs[src]["conversation_id"]})

    for i in sorted(convs):
        cv, defect = convs[i], by_conv.get(i)
        turns = cv["turns"]
        if defect == "pii":
            kind = PII_KINDS[len(expected["pii"]) % len(PII_KINDS)]
            value = _pii_value(kind, cv["lang"], rng)
            turns[0]["content"] = _PII_LEAD[cv["lang"]][kind].format(v=value) + turns[0]["content"]
            expected["pii"].append({"conversation_id": cv["conversation_id"], "turn_idx": 0,
                                    "type": kind, "value": value})
        elif defect == "toxic":
            turns[0]["content"] = _TOXIC[cv["lang"]] + turns[0]["content"]
            expected["toxic"].append({"conversation_id": cv["conversation_id"], "turn_idx": 0})
        elif defect == "leaked_think":
            turns[1]["content"] = _THINK + turns[1]["content"]
            expected["leaked_think"].append({"conversation_id": cv["conversation_id"], "turn_idx": 1})
        elif defect == "eval_contamination":
            ep = eval_prompts[len(expected["eval_contamination"]) % len(eval_prompts)]
            turns[0]["content"], cv["lang"] = ep["prompt"], ep["lang"]
            for t in turns:
                t["lang"] = ep["lang"]
            expected["eval_contamination"].append({"conversation_id": cv["conversation_id"],
                                                   "prompt_id": ep["prompt_id"]})
        start = 2 * 86400 + i * 900
        order = list(range(len(turns)))
        if defect == "out_of_order":
            order = order[1:] + order[:1]  # turn 0 arrives last
            expected["out_of_order"].append({"conversation_id": cv["conversation_id"]})
        for arrival, t_idx in enumerate(order):
            t = turns[t_idx]
            meta = {"channel": cv["channel"], "intent": cv["intent"]}
            if i >= plant["schema_drift_from"]:
                meta["channel_detail"] = {"app_version": "5.2"}  # the field that appears mid-stream
            events.append({"event_id": ids("event", cv["conversation_id"], t_idx),
                           "conversation_id": cv["conversation_id"], "turn_idx": t_idx,
                           "role": t["role"], "content": t["content"], "lang": t["lang"],
                           "raw_meta": _j(meta), "ingest_ts": _ts(start + arrival * 45)})
        ticket = {"ticket_id": ids("ticket", i), "customer_id": cv["customer_id"],
                  "conversation_id": cv["conversation_id"], "intent": cv["intent"], "status": "open"}
        crm.append(_cdc("c", None, ticket, start, "crm", "tickets"))
        crm.append(_cdc("u", ticket, {**ticket, "status": "resolved", "csat": rng.randint(1, 5)},
                        start + 600, "crm", "tickets"))

    events.sort(key=lambda e: e["ingest_ts"])
    drift = [e["event_id"] for e in events if "channel_detail" in e["raw_meta"]]
    resend = rng.sample(events, k=plant["duplicate_resend"])
    for e in resend:
        events.append(dict(e))
    events.sort(key=lambda e: (e["ingest_ts"], e["event_id"]))

    for i in range(size["web_events"]):
        cust = rng.choice(customers) if rng.random() < 0.7 else None
        etype = rng.choice(["page_view", "page_view", "search", "add_to_cart", "checkout"])
        sku = rng.choice(skus)["sku"]
        web.append({"event_id": ids("web", i), "session_id": ids("session", i // 6),
                    "customer_id": cust["customer_id"] if cust else None, "event_type": etype,
                    "page": f"/product/{sku}" if etype != "search" else "/search",
                    "payload": _j({"sku": sku} if etype != "search" else {"q": rng.choice(_PRODUCTS)[1]}),
                    "event_ts": _ts(86400 + i * 37)})

    expected_defects = {
        **{d: {"count": len(expected[d]), "items": expected[d]} for d in CONVERSATION_DEFECTS},
        "duplicate_resend": {"count": len(resend), "items": sorted(e["event_id"] for e in resend)},
        "schema_drift": {"count": len(drift), "field": "raw_meta.channel_detail",
                         "first_conversation_index": plant["schema_drift_from"]},
    }
    return {"tables": {"conversation_events": events, "crm_cdc": crm, "erp_cdc": erp,
                       "web_events": web},
            "eval_prompts": eval_prompts, "expected_defects": expected_defects,
            "seed": seed, "scale": scale}


def _conversation(i, rng, ids, customers, orders, skus) -> dict:
    cust = customers[i % len(customers)]
    lang = cust["lang"]
    intent = INTENTS[rng.randrange(len(INTENTS))]
    own = [o for o in orders if o["customer_id"] == cust["customer_id"]] or orders
    o = rng.choice(own)
    p_idx = next(s["product"] for s in skus if s["sku"] == o["sku"])
    p = _PRODUCTS[p_idx][0 if lang == "zh-TW" else 1]
    s = rng.choice(_STORES)[0 if lang == "zh-TW" else 1]
    q, a = rng.choice(_TEMPLATES[(intent, lang)])
    # The case reference is what keeps two clean conversations from being exact copies: a promo
    # or stock question carries no order id, so the same product and template would otherwise
    # repeat verbatim -- an unplanted duplicate that a correct dedup rule would be blamed for.
    case = f"（案件編號 C{i:05d}）" if lang == "zh-TW" else f" (case C{i:05d})"
    turns = [{"role": "customer", "content": q.format(o=o["order_id"], p=p, s=s), "lang": lang},
             {"role": "agent", "content": a.format(o=o["order_id"], p=p, s=s) + case, "lang": lang}]
    if rng.random() < 0.5:
        fq, fa = _FOLLOW[lang]
        turns += [{"role": "customer", "content": fq, "lang": lang},
                  {"role": "agent", "content": fa, "lang": lang}]
    return {"conversation_id": ids("conversation", i), "customer_id": cust["customer_id"],
            "lang": lang, "intent": intent, "channel": rng.choice(["chat", "chat", "email"]),
            "turns": turns}


def _near_duplicate(src: dict, i: int, ids) -> dict:
    turns = [dict(t) for t in src["turns"]]
    for a, b in _NEAR_DUP_SWAP:
        if a in turns[0]["content"]:
            turns[0]["content"] = turns[0]["content"].replace(a, b, 1)
            break
    else:
        turns[0]["content"] = turns[0]["content"].rstrip("。？?.") + ("！" if src["lang"] == "zh-TW" else "!")
    return {**src, "conversation_id": ids("conversation", i), "turns": turns}


# ── output ───────────────────────────────────────────────────────────────────
def write(dataset: dict, out: pathlib.Path) -> dict:
    """bronze_<table>.jsonl, eval_prompts.jsonl, expected_defects.json and manifest.json, each
    canonical (sorted keys, no whitespace, UTF-8), so the sha256 is a property of the content."""
    out.mkdir(parents=True, exist_ok=True)
    files = {f"bronze_{t}.jsonl": rows for t, rows in dataset["tables"].items()}
    files["eval_prompts.jsonl"] = dataset["eval_prompts"]
    hashes = {}
    for name, rows in files.items():
        body = "".join(_j(r) + "\n" for r in rows).encode()
        (out / name).write_bytes(body)
        hashes[name] = hashlib.sha256(body).hexdigest()
    body = (_j(dataset["expected_defects"]) + "\n").encode()
    (out / "expected_defects.json").write_bytes(body)
    hashes["expected_defects.json"] = hashlib.sha256(body).hexdigest()
    manifest = {"generator_version": GENERATOR_VERSION, "seed": dataset["seed"],
                "scale": dataset["scale"],
                "counts": {t: len(r) for t, r in dataset["tables"].items()} | {
                    "eval_prompts": len(dataset["eval_prompts"])},
                "defect_counts": {d: v["count"] for d, v in dataset["expected_defects"].items()},
                "sha256": hashes,
                "dataset_sha256": hashlib.sha256("".join(
                    f"{k}={v}\n" for k, v in sorted(hashes.items())).encode()).hexdigest()}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                                       encoding="utf-8")
    return manifest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--scale", choices=sorted(SCALES), default="demo")
    ap.add_argument("--out", required=True, type=pathlib.Path)
    args = ap.parse_args(argv)
    manifest = write(generate(args.seed, args.scale), args.out)
    print(json.dumps({k: manifest[k] for k in ("seed", "scale", "counts", "defect_counts",
                                              "dataset_sha256")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
