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

GENERATOR_VERSION = "2"  # 2: paraphrase families and a held-out acceptance set (PR-11)
NS = uuid.UUID("6f1d3c3e-7c4b-4e62-9a43-1b2f0c5d8e71")  # fixed namespace for uuid5 ids
BASE = _dt.datetime(2026, 9, 1, tzinfo=_dt.timezone.utc)

SCALES = {
    "small": {"customers": 40, "skus": 20, "orders": 120, "conversations": 80,
              "web_events": 300, "eval_prompts": 40},
    "demo": {"customers": 200, "skus": 50, "orders": 600, "conversations": 300,
             "web_events": 2000, "eval_prompts": 120},
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

#: Paraphrase families (generator v2, Phase 1 PR-11). Each (intent, language) has ONE policy answer
#: and several phrasings of the question. Training conversations use only the "train" phrasings; the
#: acceptance set (eval_prompts.jsonl, with a reference answer per row) uses only the "held_out" ones.
#: So an acceptance item is a question the student never saw worded that way, whose correct answer
#: follows from the same policy it did see -- a measure of generalisation, not of template recall.
#: v1 drew training rows and its eval split from one pool of 1-2 templates per intent, and measured
#: on that data, every eval row was a near-copy of a training row (median similarity 0.935).
#: {o} order, {p} product, {s} store.
_FAMILIES = {
    ("order_status", "zh-TW"): {
        "answer": "您好，訂單 {o} 已出貨，預計兩天內送達。",
        "train": ["請問訂單 {o} 目前寄到哪裡了？", "我的訂單 {o} 什麼時候會到？", "想查一下訂單 {o} 的配送進度。",
                  "訂單 {o} 出貨了嗎？"],
        "held_out": ["可以幫我看看 {o} 這筆訂單送到哪了嗎？", "{o} 這張單大概哪天會收到？"]},
    ("order_status", "en"): {
        "answer": "Your order {o} has shipped and should arrive within two days.",
        "train": ["Where is my order {o} right now?", "When will order {o} arrive?",
                  "Can you check the delivery status of order {o}?", "Has order {o} shipped yet?"],
        "held_out": ["Any update on where {o} is?", "I'm still waiting on {o}, when should it show up?"]},
    ("return_request", "zh-TW"): {
        "answer": "很抱歉造成不便，已為訂單 {o} 建立退貨單，將派員到府收取{p}。",
        "train": ["我想退貨，訂單 {o} 的{p}有瑕疵。", "{p}尺寸不合，訂單 {o} 可以退嗎？",
                  "訂單 {o} 的{p}收到就壞了，要怎麼退？", "請幫我把訂單 {o} 的{p}退掉。"],
        "held_out": ["{o} 買的{p}不想要了，能辦退貨嗎？", "剛收到的{p}有問題，訂單 {o}，我要退。"]},
    ("return_request", "en"): {
        "answer": "Sorry about that. A return for order {o} is filed and a courier will collect the {p}.",
        "train": ["I want to return the {p} from order {o}, it is defective.",
                  "The {p} in order {o} does not fit, can I return it?",
                  "The {p} from order {o} arrived broken, how do I send it back?",
                  "Please start a return for the {p} on order {o}."],
        "held_out": ["Can I still send back the {p} I got with {o}?", "{o}: the {p} is faulty and I'd like a return."]},
    ("stock_check", "zh-TW"): {
        "answer": "{s}目前{p}尚有庫存，歡迎到店選購。",
        "train": ["{s}還有{p}的庫存嗎？", "請問{s}現在有{p}的現貨嗎？", "我想去{s}買{p}，還有貨嗎？",
                  "{s}的{p}還買得到嗎？"],
        "held_out": ["今天去{s}拿得到{p}嗎？", "{p}在{s}缺貨了嗎？"]},
    ("stock_check", "en"): {
        "answer": "{s} has the {p} in stock right now.",
        "train": ["Does {s} still have the {p} in stock?", "Is the {p} available at {s}?",
                  "I want to pick up the {p} at {s}, is there stock?", "Can I buy the {p} at {s} today?"],
        "held_out": ["Would I find the {p} on the shelf at {s}?", "Is {s} out of the {p}?"]},
    ("promo_question", "zh-TW"): {
        "answer": "可以，{p}的週年慶折扣與會員點數可以同時使用。",
        "train": ["{p}的週年慶折扣可以和會員點數一起用嗎？", "買{p}時週年慶折扣跟點數能疊加嗎？",
                  "{p}週年慶打折還能用點數折抵嗎？", "會員點數可以搭配{p}的週年慶優惠嗎？"],
        "held_out": ["{p}已經是週年慶價了，點數還能再扣嗎？", "週年慶買{p}，點數跟折扣可以一起算嗎？"]},
    ("promo_question", "en"): {
        "answer": "Yes, the anniversary discount on the {p} can be combined with member points.",
        "train": ["Can the anniversary discount on the {p} be combined with member points?",
                  "Do member points stack with the anniversary deal on the {p}?",
                  "Can I use points on the {p} while it is on the anniversary discount?",
                  "Is the {p} anniversary discount compatible with member points?"],
        "held_out": ["The {p} is already discounted for the anniversary; can I still apply my points?",
                     "Points plus the anniversary price on the {p}, does that work?"]},
    ("complaint", "zh-TW"): {
        "answer": "非常抱歉，我們已記錄訂單 {o} 的客訴，主管會在一天內回覆您。",
        "train": ["訂單 {o} 的配送員態度很差，我要客訴。", "我要投訴訂單 {o} 的送貨人員。",
                  "訂單 {o} 的物流服務太糟了，我要反映。", "對訂單 {o} 的配送很不滿意，請處理。"],
        "held_out": ["{o} 送貨的人很沒禮貌，這要跟誰反映？", "針對 {o} 的配送我想正式申訴。"]},
    ("complaint", "en"): {
        "answer": "We are very sorry. The complaint on order {o} is logged; a supervisor will reply within a day.",
        "train": ["The courier for order {o} was rude, I want to complain.",
                  "I want to file a complaint about the delivery of order {o}.",
                  "The delivery service for order {o} was terrible.", "Please escalate my complaint about order {o}."],
        "held_out": ["Who do I talk to about the rude driver on {o}?", "I'd like to lodge a formal complaint regarding {o}."]},
}
_FOLLOW = {"zh-TW": ("好的，謝謝。", "不客氣，祝您有美好的一天！"), "en": ("Great, thanks.", "You're welcome, have a nice day!")}
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

    eval_prompts = _acceptance_set(size["eval_prompts"], rng, ids, orders, skus)

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
        # Not an accident of the plant: every agent reply ends in the CRM's case reference, which the
        # customer never gave (see _conversation). Recorded so strip_ungrounded_ids is held to it.
        "system_assigned_id": {"count": len(convs), "items": sorted(
            ({"conversation_id": c["conversation_id"]} for c in convs.values()),
            key=lambda i: i["conversation_id"])},
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
    fam = _FAMILIES[(intent, lang)]
    q, a = rng.choice(fam["train"]), fam["answer"]
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


def _acceptance_set(n: int, rng, ids, orders, skus) -> list[dict]:
    """n held-out items, {prompt_id, lang, intent, prompt, reference}: an equal share per intent,
    60% zh-TW, every prompt distinct, phrased only in its family's held-out wording, and the
    reference is the family's policy answer for those slots -- with no case reference, which a
    customer's question never carries."""
    items, seen = [], set()
    for k in range(n):
        intent, lang = INTENTS[k % len(INTENTS)], ("zh-TW" if (k // len(INTENTS)) % 5 < 3 else "en")
        fam = _FAMILIES[(intent, lang)]
        for _ in range(1000):
            o = rng.choice(orders)
            p = _PRODUCTS[next(s["product"] for s in skus if s["sku"] == o["sku"])][0 if lang == "zh-TW" else 1]
            st = rng.choice(_STORES)[0 if lang == "zh-TW" else 1]
            q = rng.choice(fam["held_out"]).format(o=o["order_id"], p=p, s=st)
            if q not in seen:
                break
        else:
            raise ValueError(f"cannot draw {n} distinct held-out prompts for {intent}/{lang}")
        seen.add(q)
        items.append({"prompt_id": ids("eval", k), "lang": lang, "intent": intent, "prompt": q,
                      "reference": fam["answer"].format(o=o["order_id"], p=p, s=st)})
    return items


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
