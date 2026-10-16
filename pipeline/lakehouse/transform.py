#!/usr/bin/env python3
"""Bronze → Silver → Gold as pure functions (Phase 1 PR-07; research report §4.3).

The Glue jobs (pipeline/lakehouse/glue/) are thin: they read the tables, call these functions,
and write the results. Everything that decides what the training data is happens here, where it
can be tested offline against pipeline/lakehouse/generate.py's planted defects -- every rule's
catch must equal the plant exactly.

Bronze → Silver, one named rule per defect class; the quality report records what each caught:

  dedup_event_id    a resent event (same event_id) is kept once
  tolerate_drift    raw_meta keys beyond the known ones are recorded, not fatal
  order_by_turn     turns are ordered by turn_idx, never by ingest_ts
  strip_think       <think>…</think> traces are removed from agent replies
  drop_toxic        a pair whose question is abusive is dropped
  redact_pii        structured identifiers (checksum-validated regex) and names (an injected
                    detector -- Bedrock Guardrails in the job, per spike S4) become placeholders
  decontaminate     a pair whose question is an eval prompt is dropped
  near_dup          the same case resubmitted with a small edit is kept once

near_dup keys on the case reference, not on text similarity alone. Measured on the generator's
demo scale: planted near-duplicates reach a character-trigram Jaccard as low as 0.878 while
DIFFERENT clean conversations sharing a template reach 0.945, so no similarity threshold separates
them. A resubmitted ticket keeps its case number; template mates do not share one. Reducing
template-level redundancy (MinHash diversity filtering) is a separate quality decision.

Silver → Gold: a deterministic 90/10 train/eval split by pair_id, so a row never changes side
between runs.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict

KNOWN_META_KEYS = {"channel", "intent"}
TOXIC_LEXICON = ("白痴", "笨蛋", "廢物", "idiot", "stupid", "useless")
_THINK = re.compile(r"<think>.*?</think>", re.S)
_CASE = re.compile(r"\bC[0-9]{5}\b")
NEAR_DUP_MIN_SIMILARITY = 0.8
EVAL_MODULO = 10  # 1 in 10 pairs goes to eval

# Silver/Gold data-quality rules (AWS Glue Data Quality DQDL), evaluated by the jobs.
SILVER_DQDL = ('Rules = [ IsComplete "pair_id", IsComplete "instruction", IsComplete "output", '
               'Uniqueness "pair_id" = 1.0, RowCount > 0, '
               'CustomSql "SELECT COUNT(*) FROM primary WHERE output LIKE \'%<think>%\'" = 0 ]')
GOLD_DQDL = None  # built below, once the redaction tokens are defined


# ── PII: structured identifiers (S4's validated layer) ───────────────────────
_ID_LETTER = {c: v for c, v in zip("ABCDEFGHJKLMNPQRSTUVXYWZIO", range(10, 36))}


def tw_id_ok(s: str) -> bool:
    if not re.fullmatch(r"[A-Z][12][0-9]{8}", s):
        return False
    n = _ID_LETTER[s[0]]
    digits = [n // 10, n % 10] + [int(c) for c in s[1:]]
    return sum(d * w for d, w in zip(digits, [1, 9, 8, 7, 6, 5, 4, 3, 2, 1, 1])) % 10 == 0


def ubn_ok(s: str) -> bool:
    total = 0
    for d, w in zip(map(int, s), (1, 2, 1, 2, 1, 2, 4, 1)):
        p = d * w
        total += p // 10 + p % 10
    return total % 5 == 0 or (s[6] == "7" and (total + 1) % 5 == 0)


def luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for c in reversed(digits):
        d = int(c)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total, alt = total + d, not alt
    return total % 10 == 0


#: (placeholder, pattern, validator). Validators keep a random digit run from being redacted as
#: an identifier it only resembles -- the precision half of S4's 1.0/1.0.
STRUCTURED = [
    ("[TW_ID]", re.compile(r"(?<![A-Za-z0-9])[A-Z][12][0-9]{8}(?![0-9])"), tw_id_ok),
    ("[PHONE]", re.compile(r"(?<![0-9])(?:\+886-?9[0-9]{2}-?[0-9]{3}-?[0-9]{3}|09[0-9]{2}-?[0-9]{3}-?[0-9]{3})(?![0-9])"), None),
    ("[PHONE]", re.compile(r"(?:\(0[2-8]\)|(?<![0-9])0[2-8]-)[0-9]{3,4}-[0-9]{4}(?![0-9])"), None),
    ("[EMAIL]", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), None),
    ("[UBN]", re.compile(r"(?:(?<=統一編號)|(?<=統編))\s*[0-9]{8}(?![0-9])"), lambda s: ubn_ok(s.strip())),
    ("[CARD]", re.compile(r"(?<![0-9])(?:[0-9]{4}[- ]){3}[0-9]{4}(?![0-9])"),
     lambda s: luhn_ok(re.sub(r"\D", "", s))),
]


def redact_structured(text: str) -> tuple[str, list[str]]:
    found = []
    for placeholder, rx, check in STRUCTURED:
        def sub(m, placeholder=placeholder, check=check):
            if check is None or check(m.group(0)):
                found.append(placeholder)
                return placeholder
            return m.group(0)
        text = rx.sub(sub, text)
    return text, found


def org_entities(reference: dict) -> frozenset:
    """Every store and product name, in both languages, from the org-entities reference data."""
    return frozenset(v for kind in ("stores", "products") for e in reference.get(kind, []) for v in e.values() if v)


def redact(text: str, name_detector=None, keep: frozenset = frozenset()) -> tuple[str, list[str]]:
    """Structured identifiers first (deterministic), then names from `name_detector(text)`, a
    callable returning the name strings it found (Bedrock Guardrails in the Glue job).

    A detected name inside an organisation-owned entity present in the same text (`keep`: store
    and product names) is not a person and is left alone. Live, the demo-scale smoke found
    Guardrails reading Taiwanese district-named stores (高雄前鎮店, Taichung Xitun) as NAME."""
    text, found = redact_structured(text)
    present = [e for e in keep if e in text]
    for name in sorted(set(name_detector(text) if name_detector else []), key=len, reverse=True):
        if any(name in e for e in present):
            continue
        if name and name in text:
            text = text.replace(name, "[NAME]")
            found.append("[NAME]")
    return text, found


# ── Bronze → Silver ──────────────────────────────────────────────────────────
def pair_id(instruction: str, inp: str, output: str) -> str:
    return hashlib.sha256("\x1f".join((instruction, inp, output)).encode()).hexdigest()


def _norm(s: str) -> str:
    return " ".join(s.split()).strip()


def _similarity(a: str, b: str) -> float:
    """Character-sequence similarity. Not trigram Jaccard: on a short Chinese question one changed
    character costs three trigrams -- 請問→請教 scored 0.667 -- while the sequence ratio is ~0.92."""
    import difflib
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def bronze_to_silver(events: list[dict], eval_prompts: list[str], *, name_detector=None,
                     keep_entities: frozenset = frozenset(), dq_run_id: str = "",
                     updated_ts: str = "") -> tuple[list[dict], dict]:
    """Conversation events → instruction pairs, plus a report of what each rule caught.
    A pair is (first customer turn, first agent turn); later turns are pleasantries."""
    report = {r: [] for r in ("dedup_event_id", "tolerate_drift", "order_by_turn", "strip_think",
                              "drop_toxic", "redact_pii", "decontaminate", "near_dup")}
    seen, unique = set(), []
    for e in events:
        if e["event_id"] in seen:
            if e["event_id"] not in report["dedup_event_id"]:
                report["dedup_event_id"].append(e["event_id"])
            continue
        seen.add(e["event_id"])
        unique.append(e)
        extra = set(json.loads(e["raw_meta"] or "{}")) - KNOWN_META_KEYS
        if extra:
            report["tolerate_drift"].append(e["event_id"])

    convs = defaultdict(list)
    for e in unique:
        convs[e["conversation_id"]].append(e)
    evals = {_norm(p) for p in eval_prompts}
    candidates = []
    for cid, turns in convs.items():
        arrival = [t["turn_idx"] for t in sorted(turns, key=lambda t: (t["ingest_ts"], t["turn_idx"]))]
        ordered = sorted(turns, key=lambda t: t["turn_idx"])
        if arrival != [t["turn_idx"] for t in ordered]:
            report["order_by_turn"].append(cid)
        q = next((t for t in ordered if t["role"] == "customer"), None)
        a = next((t for t in ordered if t["role"] == "agent" and t["turn_idx"] > (q or {}).get("turn_idx", -1)), None)
        if q is None or a is None:
            continue
        output = a["content"]
        if _THINK.search(output):
            output = _norm(_THINK.sub("", output))
            report["strip_think"].append(cid)
        question = q["content"]
        if any(w in question.lower() for w in TOXIC_LEXICON):
            report["drop_toxic"].append(cid)
            continue
        if _norm(question) in evals:
            report["decontaminate"].append(cid)
            continue
        instruction, f1 = redact(question, name_detector, keep_entities)
        output, f2 = redact(output, name_detector, keep_entities)
        if f1 or f2:
            report["redact_pii"].append(cid)
        candidates.append({"cid": cid, "ts": min(t["ingest_ts"] for t in turns), "lang": q.get("lang") or "",
                           "instruction": instruction, "output": output,
                           "pii": bool(f1 or f2)})

    by_case = defaultdict(list)
    for c in candidates:
        case = _CASE.search(c["output"])
        by_case[case.group(0) if case else c["cid"]].append(c)
    keep = []
    for group in by_case.values():
        group.sort(key=lambda c: (c["ts"], c["cid"]))
        kept = [group[0]]
        for c in group[1:]:
            if _similarity(c["instruction"], kept[0]["instruction"]) >= NEAR_DUP_MIN_SIMILARITY:
                report["near_dup"].append(c["cid"])
            else:
                kept.append(c)
        keep += kept

    rows = []
    for c in sorted(keep, key=lambda c: (c["ts"], c["cid"])):
        rows.append({"pair_id": pair_id(c["instruction"], "", c["output"]), "instruction": c["instruction"],
                     "input": "", "output": c["output"], "lang": c["lang"], "source_ref": c["cid"],
                     "pii_status": "redacted" if c["pii"] else "clean",
                     "redaction_method": "regex+guardrails" if c["pii"] else None,
                     "dq_run_id": dq_run_id or None, "updated_ts": updated_ts})
    summary = {"input_events": len(events), "unique_events": len(unique), "conversations": len(convs),
               "pairs": len(rows), "rules": {k: {"count": len(v), "items": sorted(v)} for k, v in report.items()}}
    return rows, summary


# ── Silver → Gold ────────────────────────────────────────────────────────────
def split_of(pid: str) -> str:
    return "eval" if int(pid[:8], 16) % EVAL_MODULO == 0 else "train"


def redaction_tokens() -> list[str]:
    """Every placeholder the Silver redaction writes."""
    return sorted({token for token, _, _ in STRUCTURED} | {"[NAME]"})


def carries_redaction(pair: dict) -> bool:
    text = " ".join(str(pair.get(k) or "") for k in ("instruction", "input", "output"))
    return any(t in text for t in redaction_tokens())


def silver_to_gold(pairs: list[dict]) -> tuple[list[dict], list[dict]]:
    """(train, eval) rows in the Gold contract: pair_id, instruction, input, output, split.

    A pair that still carries a redaction placeholder stays in Silver (the governed, audited record
    of what redaction did) and is NOT promoted to Gold. Redacted text is still training text: live
    (PR-11) a student trained on 27 such rows coined an `[ORDERID]` of its own in 2 of 120 held-out
    answers, the bracket style learned as output, and the format gate failed on exactly those. Gold
    is where training data is chosen, so that is where the exclusion lives, and GOLD_DQDL asserts it."""
    train, ev = [], []
    for p in pairs:
        if carries_redaction(p):
            continue
        row = {"pair_id": p["pair_id"], "instruction": p["instruction"], "input": p["input"] or "",
               "output": p["output"], "split": split_of(p["pair_id"])}
        (ev if row["split"] == "eval" else train).append(row)
    return train, ev


def to_messages(row: dict) -> dict:
    """Gold row → the chat `messages` record train_qlora.py consumes (§2: instruction/input/output
    → messages, a pure function with its own test)."""
    user = row["instruction"] + (f"\n\n{row['input']}" if row.get("input") else "")
    return {"messages": [{"role": "user", "content": user}, {"role": "assistant", "content": row["output"]}]}


# ── the contract with the generator ──────────────────────────────────────────
#: rule -> the generator defect it must catch exactly
RULE_FOR_DEFECT = {"dedup_event_id": "duplicate_resend", "tolerate_drift": "schema_drift",
                   "order_by_turn": "out_of_order", "strip_think": "leaked_think", "drop_toxic": "toxic",
                   "redact_pii": "pii", "decontaminate": "eval_contamination", "near_dup": "near_duplicate"}


def compare_to_plants(rules: dict, expected: dict) -> dict:
    """{rule: (passed, detail)}: each rule's catch against the generator's expected_defects.
    Sets are compared where the plant names conversations or events; schema drift and near
    duplicates by count, plus, for near duplicates, exactly one side of every planted pair."""
    out = {}
    for rule, defect in RULE_FOR_DEFECT.items():
        caught, plant = set(rules[rule]["items"]), expected[defect]
        if defect == "schema_drift":
            ok = rules[rule]["count"] == plant["count"]
        elif defect == "duplicate_resend":
            ok = caught == set(plant["items"])
        elif defect == "near_duplicate":
            ok = len(caught) == plant["count"] and all(
                (i["conversation_id"] in caught) != (i["copy_of"] in caught) for i in plant["items"])
        else:
            ok = caught == {i["conversation_id"] for i in plant["items"]}
        out[rule] = (ok, f"caught {rules[rule]['count']}, planted {plant['count']}")
    return out


def _gold_dqdl() -> str:
    """LIKE over the exact token list rather than a regex: Spark's LIKE treats brackets literally
    (only % and _ are special, and an `_` matching any character only widens the check)."""
    hits = " OR ".join(f"{col} LIKE '%{t}%'" for t in redaction_tokens() for col in ("instruction", "output"))
    return ('Rules = [ IsComplete "instruction", IsComplete "output", Uniqueness "pair_id" = 1.0, '
            'ColumnValues "split" in ["train", "eval"], RowCount > 0, '
            f'CustomSql "SELECT COUNT(*) FROM primary WHERE {hits}" = 0 ]')


GOLD_DQDL = _gold_dqdl()
