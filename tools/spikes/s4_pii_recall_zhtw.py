#!/usr/bin/env python3
"""S4 — PII detection recall on Traditional Chinese and English customer-service text.

Which detector, or which layering of detectors, catches PII in zh-TW and EN text? 1,000 seeded
synthetic records — half zh-TW, half EN — carry planted entities at known character offsets:
checksum-valid Taiwanese national IDs, mobile numbers in three formats, landlines, emails,
checksum-valid business tax ids (統一編號), Luhn-valid card numbers written with separators,
Chinese and English names, and addresses. Three detectors are scored on span overlap:

  D1 regex      a local zh-TW pattern layer (checksum / Luhn validated) — what PR-07 would ship
  D2 guardrail  Amazon Bedrock Guardrails sensitive-information filter, built-in PII types only,
                ANONYMIZE, via ApplyGuardrail (no model call)
  D3 glue       AWS Glue 5 Detect PII (EntityDetector.detect, HIGH sensitivity), incl. the
                managed TAIWAN_NATIONAL_IDENTIFICATION_NUMBER type

The open-source privacy-filter model named in the report needs the models mirror first (the
pipeline's mirror_model task — pipeline work, not a spike), so it is NOT RUN here and recorded
as a follow-up rather than left out silently.

Every non-empty combination of the detectors that ran is scored; the best layered combination
is judged against the declared criteria. Committed evidence carries aggregates only: the records
exist at run time, in memory and under s3://<DATA_BUCKET>/spikes/s4/, which teardown deletes.

Gates: Phase 1 PR-07 (the PII layering in Bronze -> Silver).

    python3 tools/spikes/s4_pii_recall_zhtw.py plan
    python3 tools/spikes/s4_pii_recall_zhtw.py run
"""
from __future__ import annotations

import itertools
import json
import pathlib
import random
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _spike  # noqa: E402
from _spike import Spike, main  # noqa: E402

N_RECORDS = 1000
SEED = 20261013
PREFIX = "spikes/s4"
GUARDRAIL_NAME = "llmops-spike-pii"
GLUE_ROLE = "llmops-spike-s4-glue"
GLUE_JOB = "llmops-spike-s4-detect-pii"

STRUCTURED = ("TW_ID", "TW_MOBILE", "TW_LANDLINE", "EMAIL", "TW_UBN", "CARD")
NAMES = ("NAME_ZH", "NAME_EN")
ADDRESSES = ("ADDRESS_ZH", "ADDRESS_EN")
ALL_TYPES = STRUCTURED + NAMES + ADDRESSES

GLUE_ENTITY_TYPES = ["PERSON_NAME", "EMAIL", "PHONE_NUMBER", "CREDIT_CARD", "IP_ADDRESS",
                     "TAIWAN_NATIONAL_IDENTIFICATION_NUMBER", "TAIWAN_PASSPORT_NUMBER",
                     "CHINA_PHONE_NUMBER", "CHINA_IDENTIFICATION"]
GUARDRAIL_PII = ["NAME", "EMAIL", "PHONE", "ADDRESS", "CREDIT_DEBIT_CARD_NUMBER",
                 "CREDIT_DEBIT_CARD_CVV", "CREDIT_DEBIT_CARD_EXPIRY", "PIN", "PASSWORD",
                 "DRIVER_ID", "LICENSE_PLATE", "INTERNATIONAL_BANK_ACCOUNT_NUMBER"]

# ── synthetic data (seeded, fictional) ───────────────────────────────────────
_SURNAMES = "陳林黃張李王吳劉蔡楊許鄭謝洪郭邱曾廖賴徐"
_GIVEN = ["志明", "淑芬", "家豪", "雅婷", "冠宇", "怡君", "俊傑", "佩珊", "建宏", "欣怡", "宗翰", "美玲"]
_EN_FIRST = ["Kevin", "Grace", "Jason", "Emily", "Daniel", "Vivian", "Eric", "Joanne", "Allen", "Tina"]
_EN_LAST = ["Chen", "Lin", "Huang", "Chang", "Lee", "Wang", "Wu", "Liu", "Tsai", "Yang"]
_CITIES = [("台北市", "信義區", "松仁路"), ("新北市", "板橋區", "文化路"), ("台中市", "西屯區", "台灣大道"),
           ("高雄市", "前鎮區", "中華五路"), ("台南市", "東區", "林森路")]
_EN_ROADS = [("Songren Rd", "Xinyi Dist", "Taipei"), ("Wenhua Rd", "Banqiao Dist", "New Taipei"),
             ("Taiwan Blvd", "Xitun Dist", "Taichung"), ("Zhonghua 5th Rd", "Qianzhen Dist", "Kaohsiung")]
_ID_LETTER = {c: v for c, v in zip("ABCDEFGHJKLMNPQRSTUVXYWZIO",
                                   [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25,
                                    26, 27, 28, 29, 30, 31, 32, 33, 34, 35])}


def tw_id_checksum_ok(s: str) -> bool:
    """Taiwanese national ID: letter + [12] + 8 digits; weighted checksum mod 10 == 0."""
    if not re.fullmatch(r"[A-Z][12][0-9]{8}", s):
        return False
    n = _ID_LETTER[s[0]]
    digits = [n // 10, n % 10] + [int(c) for c in s[1:]]
    weights = [1, 9, 8, 7, 6, 5, 4, 3, 2, 1, 1]
    return sum(d * w for d, w in zip(digits, weights)) % 10 == 0


def make_tw_id(rng: random.Random) -> str:
    while True:
        s = rng.choice("ABCDEFGHJKLMNPQRSTUVXYWZ") + rng.choice("12") + "".join(
            rng.choice("0123456789") for _ in range(8))
        if tw_id_checksum_ok(s):
            return s


def ubn_checksum_ok(s: str) -> bool:
    """統一編號 (8 digits), weights 1,2,1,2,1,2,4,1; the 7th digit 7 rule allows +1."""
    if not re.fullmatch(r"[0-9]{8}", s):
        return False
    total = 0
    for d, w in zip(map(int, s), (1, 2, 1, 2, 1, 2, 4, 1)):
        p = d * w
        total += p // 10 + p % 10
    return total % 5 == 0 or (s[6] == "7" and (total + 1) % 5 == 0)


def make_ubn(rng: random.Random) -> str:
    while True:
        s = "".join(rng.choice("0123456789") for _ in range(8))
        if ubn_checksum_ok(s):
            return s


def luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for c in reversed(digits):
        d = int(c)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total, alt = total + d, not alt
    return total % 10 == 0


def make_card(rng: random.Random) -> str:
    """16 Luhn-valid digits, always written with separators (never a 12-digit run)."""
    while True:
        d = "4" + "".join(rng.choice("0123456789") for _ in range(15))
        if luhn_ok(d):
            sep = rng.choice(["-", " "])
            return sep.join(d[i:i + 4] for i in range(0, 16, 4))


def make_entity(kind: str, rng: random.Random) -> str:
    if kind == "TW_ID":
        return make_tw_id(rng)
    if kind == "TW_MOBILE":
        n = "9" + "".join(rng.choice("0123456789") for _ in range(8))
        return rng.choice([f"0{n[:3]}-{n[3:6]}-{n[6:]}", f"0{n}", f"+886-{n[:3]}-{n[3:6]}-{n[6:]}"])
    if kind == "TW_LANDLINE":
        n = "".join(rng.choice("0123456789") for _ in range(8))
        return rng.choice([f"(02){n[:4]}-{n[4:]}", f"02-{n[:4]}-{n[4:]}"])
    if kind == "EMAIL":
        user = rng.choice(["kevin", "grace.lin", "jason_wu", "emily88", "service.tw"])
        return f"{user}{rng.randint(1, 999)}@{rng.choice(['example.com', 'mail.example.tw', 'demo.example.org'])}"
    if kind == "TW_UBN":
        return make_ubn(rng)
    if kind == "CARD":
        return make_card(rng)
    if kind == "NAME_ZH":
        return rng.choice(_SURNAMES) + rng.choice(_GIVEN)
    if kind == "NAME_EN":
        return f"{rng.choice(_EN_FIRST)} {rng.choice(_EN_LAST)}"
    if kind == "ADDRESS_ZH":
        city, dist, road = rng.choice(_CITIES)
        return f"{city}{dist}{road}{rng.randint(1, 300)}號{rng.randint(2, 20)}樓"
    if kind == "ADDRESS_EN":
        road, dist, city = rng.choice(_EN_ROADS)
        return f"No. {rng.randint(1, 300)}, {road}, {dist}, {city}"
    raise ValueError(kind)


_ZH = {"TW_ID": "我的身分證字號是{v}，", "TW_MOBILE": "手機{v}，", "TW_LANDLINE": "公司電話{v}，",
       "EMAIL": "電子郵件{v}，", "TW_UBN": "統一編號{v}，", "CARD": "信用卡號{v}，",
       "NAME_ZH": "我是{v}，", "NAME_EN": "英文名{v}，", "ADDRESS_ZH": "寄送地址{v}，", "ADDRESS_EN": "地址 {v}，"}
_EN = {"TW_ID": "my ID number is {v}, ", "TW_MOBILE": "mobile {v}, ", "TW_LANDLINE": "office line {v}, ",
       "EMAIL": "email {v}, ", "TW_UBN": "company tax ID (UBN) {v}, ", "CARD": "card number {v}, ",
       "NAME_ZH": "I am {v}, ", "NAME_EN": "this is {v}, ", "ADDRESS_ZH": "ship to {v}, ",
       "ADDRESS_EN": "ship to {v}, "}
_ZH_OPEN = ["您好，", "客服您好，", "不好意思，"]
_ZH_CLOSE = ["請協助查詢訂單進度。", "我要申請退貨。", "請問這個商品還有庫存嗎？", "促銷價格可以套用嗎？"]
_EN_OPEN = ["Hi, ", "Hello support, ", "Sorry to bother you, "]
_EN_CLOSE = ["please check my order status.", "I'd like to return an item.",
             "is this product back in stock?", "can the promo price apply?"]


def generate_records(n: int = N_RECORDS, seed: int = SEED) -> list[dict]:
    """Seeded records: {id, lang, text, truth: [{type, value, start, end}]}. Deterministic."""
    rng, out = random.Random(seed), []
    for i in range(n):
        lang = "zh-TW" if i % 2 == 0 else "en"
        tmpl, opener, closer = ((_ZH, rng.choice(_ZH_OPEN), rng.choice(_ZH_CLOSE)) if lang == "zh-TW"
                                else (_EN, rng.choice(_EN_OPEN), rng.choice(_EN_CLOSE)))
        name_kind = "NAME_ZH" if lang == "zh-TW" or rng.random() < 0.3 else "NAME_EN"
        addr_kind = "ADDRESS_ZH" if lang == "zh-TW" else "ADDRESS_EN"
        kinds = [name_kind] + rng.sample([k for k in STRUCTURED], k=rng.randint(1, 3))
        if rng.random() < 0.5:
            kinds.append(addr_kind)
        text, truth = opener, []
        for kind in kinds:
            value = make_entity(kind, rng)
            piece = tmpl[kind].format(v=value)
            start = len(text) + piece.index(value)
            truth.append({"type": kind, "value": value, "start": start, "end": start + len(value)})
            text += piece
        text += closer
        out.append({"id": i, "lang": lang, "text": text, "truth": truth})
    return out


# ── D1: the local regex layer ────────────────────────────────────────────────
_REGEXES = [
    ("TW_ID", re.compile(r"(?<![A-Za-z0-9])[A-Z][12][0-9]{8}(?![0-9])"), tw_id_checksum_ok),
    ("TW_MOBILE", re.compile(r"(?<![0-9])(?:\+886-?9[0-9]{2}-?[0-9]{3}-?[0-9]{3}|09[0-9]{2}-?[0-9]{3}-?[0-9]{3})(?![0-9])"), None),
    ("TW_LANDLINE", re.compile(r"(?:\(0[2-8]\)|(?<![0-9])0[2-8]-)[0-9]{3,4}-[0-9]{4}(?![0-9])"), None),
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), None),
    ("TW_UBN", re.compile(r"(?:統一編號|統編|UBN\)?|tax ID(?: \(UBN\))?)\s*([0-9]{8})(?![0-9])"), ubn_checksum_ok),
    ("CARD", re.compile(r"(?<![0-9])(?:[0-9]{4}[- ]){3}[0-9]{4}(?![0-9])"), lambda s: luhn_ok(re.sub(r"\D", "", s))),
]


def detect_regex(text: str) -> list[tuple[int, int]]:
    spans = []
    for _kind, rx, check in _REGEXES:
        for m in rx.finditer(text):
            g = 1 if m.groups() else 0
            value = m.group(g)
            if check is None or check(value):
                spans.append((m.start(g), m.end(g)))
    return spans


# ── scoring ──────────────────────────────────────────────────────────────────
def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def spans_from_matches(text: str, matches: list[str]) -> list[tuple[int, int]]:
    """Guardrails reports matched strings, not offsets: locate every occurrence."""
    spans = []
    for m in matches:
        m = (m or "").strip()
        if not m:
            continue
        start = text.find(m)
        while start != -1:
            spans.append((start, start + len(m)))
            start = text.find(m, start + 1)
    return spans


def score(records: list[dict], detections: dict[int, list[tuple[int, int]]]) -> dict:
    """Recall per (type, lang) and overall precision for one detector or one union of detectors."""
    caught, total = {}, {}
    tp_det = fp_det = 0
    for r in records:
        dets = detections.get(r["id"], [])
        for t in r["truth"]:
            key = (t["type"], r["lang"])
            total[key] = total.get(key, 0) + 1
            if any(_overlaps((t["start"], t["end"]), d) for d in dets):
                caught[key] = caught.get(key, 0) + 1
        for d in dets:
            if any(_overlaps((t["start"], t["end"]), d) for t in r["truth"]):
                tp_det += 1
            else:
                fp_det += 1

    def recall(types):
        n = sum(v for (k, _), v in total.items() if k in types)
        c = sum(v for (k, _), v in caught.items() if k in types)
        return round(c / n, 4) if n else None
    by_type = {f"{k}/{lang}": round(caught.get((k, lang), 0) / v, 4) for (k, lang), v in sorted(total.items())}
    ndet = tp_det + fp_det
    return {"recall_structured": recall(STRUCTURED), "recall_names": recall(NAMES),
            "recall_addresses": recall(ADDRESSES), "precision": round(tp_det / ndet, 4) if ndet else None,
            "over_redaction": round(fp_det / ndet, 4) if ndet else None,
            "detections": ndet, "by_type": by_type}


def union(*maps: dict[int, list]) -> dict[int, list]:
    out: dict[int, list] = {}
    for m in maps:
        for k, v in m.items():
            out.setdefault(k, []).extend(v)
    return out


def best_combination(records, per_detector: dict[str, dict[int, list]]) -> tuple[str, dict]:
    """Highest structured recall, then names recall, then precision, over every non-empty subset."""
    best = None
    for r in range(1, len(per_detector) + 1):
        for combo in itertools.combinations(sorted(per_detector), r):
            s = score(records, union(*(per_detector[c] for c in combo)))
            key = (s["recall_structured"] or 0, s["recall_names"] or 0, s["precision"] or 0)
            if best is None or key > best[0]:
                best = (key, "+".join(combo), s)
    return best[1], best[2]


GLUE_SCRIPT = '''
import sys
from awsglue.context import GlueContext
from awsglue.utils import getResolvedOptions
from awsglueml.transforms import EntityDetector
from pyspark.context import SparkContext
args = getResolvedOptions(sys.argv, ["input", "output", "entity_types"])
gc = GlueContext(SparkContext.getOrCreate())
frame = gc.create_dynamic_frame.from_options("s3", {"paths": [args["input"]]}, "json")
detected = EntityDetector().detect(frame, args["entity_types"].split(","), "DetectedEntities", "HIGH")
gc.write_dynamic_frame.from_options(detected.repartition(1), "s3", {"path": args["output"]}, "json")
'''


class S4PiiRecallZhtw(Spike):
    NAME = "s4_pii_recall_zhtw"
    NUMBER = 4
    QUESTION = "PII detection recall on Traditional Chinese and English text"
    GATES = "Phase 1 PR-07: the PII layering in Bronze -> Silver"
    PASS_CRITERIA = (
        ("structured", "A layered combination reaches recall >= 0.99 on structured types"),
        ("names", "Recall >= 0.95 on Chinese and English names"),
        ("precision", "Precision >= 0.9"),
        ("over_redaction", "The over-redaction rate is measured per detector"),
    )
    IMPLEMENTED = True

    def plan(self) -> list[dict]:
        return [{"kind": "bedrock guardrail", "name": f"{GUARDRAIL_NAME} (built-in PII, ANONYMIZE)"},
                {"kind": "iam role", "name": GLUE_ROLE},
                {"kind": "glue job", "name": f"{GLUE_JOB} (Glue 5, Detect PII, HIGH)"},
                {"kind": "s3 objects", "name": f"s3://<DATA_BUCKET>/{PREFIX}/ ({N_RECORDS} synthetic records)"}]

    def run(self) -> None:
        sts, ssm = self.client("sts"), self.client("ssm")
        self.account_id = self.account_id or sts.get_caller_identity()["Account"]
        bucket = ssm.get_parameter(Name="/llmops/storage/bucket")["Parameter"]["Value"]
        self.redactions.append((bucket, "<DATA_BUCKET>"))
        s3 = self.client("s3")
        self.track("s3 prefix", f"{PREFIX}/", lambda: _delete_prefix(s3, bucket, PREFIX))
        records = generate_records()
        self.observe("records", {"n": len(records), "seed": SEED,
                                 "entities": sum(len(r["truth"]) for r in records)})
        per = {"regex": {r["id"]: detect_regex(r["text"]) for r in records}}
        try:
            per["guardrail"] = self._guardrail(records)
        except Exception as e:  # noqa: BLE001 -- one detector failing must not hide the others
            self.errors.append(f"guardrail detector: {type(e).__name__}: {e}"[:300])
        try:
            per["glue"] = self._glue(s3, bucket, records)
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"glue detector: {type(e).__name__}: {e}"[:300])
        self.observe("privacy_filter", "NOT RUN: needs the model mirrored via the pipeline's "
                     "mirror_model task first; follow-up")
        self._judge(records, per)

    def _judge(self, records, per) -> None:
        singles = {name: score(records, det) for name, det in per.items()}
        for name, s in singles.items():
            self.observe(f"detector_{name}", {k: v for k, v in s.items() if k != "by_type"})
            self.observe(f"detector_{name}_by_type", s["by_type"])
        combo, s = best_combination(records, per)
        self.observe("best_combination", combo)
        self.observe("best_combination_scores", {k: v for k, v in s.items() if k != "by_type"})
        self.record("structured", (s["recall_structured"] or 0) >= 0.99, f"{combo}: {s['recall_structured']}")
        self.record("names", (s["recall_names"] or 0) >= 0.95, f"{combo}: {s['recall_names']}")
        self.record("precision", (s["precision"] or 0) >= 0.9, f"{combo}: {s['precision']}")
        self.record("over_redaction", all(v["over_redaction"] is not None for v in singles.values()),
                    ", ".join(f"{k}={v['over_redaction']}" for k, v in singles.items()))

    def _guardrail(self, records) -> dict[int, list]:
        br = self.client("bedrock")
        g = br.create_guardrail(
            name=GUARDRAIL_NAME, description="spike s4: built-in PII detection quality on zh-TW/EN",
            # Both directions explicitly. Observed live (S4 run 1, 2026-10-13): with only `action`
            # set, ApplyGuardrail(source="INPUT") returned action NONE and no sensitive-information
            # assessment at all, while source="OUTPUT" anonymized the same text.
            sensitiveInformationPolicyConfig={"piiEntitiesConfig": [
                {"type": t, "action": "ANONYMIZE", "inputAction": "ANONYMIZE", "outputAction": "ANONYMIZE",
                 "inputEnabled": True, "outputEnabled": True} for t in GUARDRAIL_PII]},
            blockedInputMessaging="blocked", blockedOutputsMessaging="blocked",
            tags=[{"key": k, "value": v} for k, v in self.tags.items()])
        gid = g["guardrailId"]
        self.track("bedrock guardrail", GUARDRAIL_NAME, lambda: br.delete_guardrail(guardrailIdentifier=gid))
        rt = self.client("bedrock-runtime")

        def matches_for(text, source):
            resp = _with_retry(lambda: rt.apply_guardrail(
                guardrailIdentifier=gid, guardrailVersion="DRAFT", source=source,
                content=[{"text": {"text": text}}]))
            found = []
            for a in resp.get("assessments", []):
                sip = a.get("sensitiveInformationPolicy", {})
                found += [e.get("match", "") for e in sip.get("piiEntities", [])]
                found += [e.get("match", "") for e in sip.get("regexes", [])]
            return found
        # Probe which source detects on this guardrail, on the first records, and say which.
        source = "INPUT"
        if not any(matches_for(r["text"], "INPUT") for r in records[:6]):
            source = "OUTPUT"
        self.observe("guardrail_source", source)
        return {r["id"]: spans_from_matches(r["text"], matches_for(r["text"], source)) for r in records}

    def _glue(self, s3, bucket, records) -> dict[int, list]:
        body = "\n".join(json.dumps({"id": r["id"], "text": r["text"]}, ensure_ascii=False) for r in records)
        s3.put_object(Bucket=bucket, Key=f"{PREFIX}/input/records.jsonl", Body=body.encode("utf-8"))
        s3.put_object(Bucket=bucket, Key=f"{PREFIX}/detect_pii.py", Body=GLUE_SCRIPT.encode())
        iam, glue = self.client("iam"), self.client("glue")
        trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
                 "Principal": {"Service": "glue.amazonaws.com"}, "Action": "sts:AssumeRole",
                 "Condition": {"StringEquals": {"aws:SourceAccount": self.account_id}}}]}
        role = iam.create_role(RoleName=GLUE_ROLE, AssumeRolePolicyDocument=json.dumps(trust),
                               **_spike.boundary_kwargs(), Tags=[{"Key": k, "Value": v} for k, v in self.tags.items()])["Role"]["Arn"]
        iam.put_role_policy(RoleName=GLUE_ROLE, PolicyName="spike", PolicyDocument=json.dumps(
            glue_role_policy(self.region, self.account_id, bucket)))

        def _del_role():
            iam.delete_role_policy(RoleName=GLUE_ROLE, PolicyName="spike")
            iam.delete_role(RoleName=GLUE_ROLE)
        self.track("iam role", GLUE_ROLE, _del_role)
        time.sleep(12)
        glue.create_job(Name=GLUE_JOB, Role=role, GlueVersion="5.0", WorkerType="G.1X", NumberOfWorkers=2,
                        Command={"Name": "glueetl", "ScriptLocation": f"s3://{bucket}/{PREFIX}/detect_pii.py",
                                 "PythonVersion": "3"}, Tags=self.tags)
        self.track("glue job", GLUE_JOB, lambda: glue.delete_job(JobName=GLUE_JOB))
        run_id = glue.start_job_run(JobName=GLUE_JOB, Arguments={
            "--input": f"s3://{bucket}/{PREFIX}/input/", "--output": f"s3://{bucket}/{PREFIX}/output/",
            "--entity_types": ",".join(GLUE_ENTITY_TYPES)})["JobRunId"]
        state, err = _wait_glue(glue, GLUE_JOB, run_id)
        self.observe("glue_job_state", f"{state} {err}".strip())
        if state != "SUCCEEDED":
            raise RuntimeError(f"Detect PII job {state}: {err}")
        return parse_glue_output(_read_prefix(s3, bucket, f"{PREFIX}/output/"))


def parse_glue_output(blobs: list[bytes]) -> dict[int, list]:
    """Glue writes one JSON object per line with DetectedEntities: {column: [{start, end, ...}]}."""
    out = {}
    for blob in blobs:
        for line in blob.decode("utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            ents = (row.get("DetectedEntities") or {}).get("text", [])
            out[int(row["id"])] = [(int(e["start"]), int(e["end"])) for e in ents]
    return out


def glue_role_policy(region: str, account_id: str, bucket: str) -> dict:
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "SpikeData", "Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"],
         "Resource": f"arn:aws:s3:::{bucket}/{PREFIX}/*"},
        {"Sid": "SpikeList", "Effect": "Allow", "Action": "s3:ListBucket",
         "Resource": f"arn:aws:s3:::{bucket}", "Condition": {"StringLike": {"s3:prefix": [f"{PREFIX}/*"]}}},
        {"Sid": "Logs", "Effect": "Allow",
         "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{region}:{account_id}:log-group:/aws-glue/*"}]}


def _with_retry(fn, attempts: int = 6):
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if "Throttl" not in str(e) or i == attempts - 1:
                raise
            time.sleep(2 ** i)


def _read_prefix(s3, bucket, prefix) -> list[bytes]:
    keys = [o["Key"] for p in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
            for o in p.get("Contents", []) if not o["Key"].endswith("/")]
    return [s3.get_object(Bucket=bucket, Key=k)["Body"].read() for k in keys]


def _delete_prefix(s3, bucket, prefix) -> None:
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix + "/"):
        objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if objs:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objs})


def _wait_glue(glue, job, run_id, timeout_s=1800):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        jr = glue.get_job_run(JobName=job, RunId=run_id)["JobRun"]
        if jr["JobRunState"] in ("SUCCEEDED", "FAILED", "ERROR", "TIMEOUT", "STOPPED"):
            return jr["JobRunState"], jr.get("ErrorMessage", "")
        time.sleep(20)
    return "TIMEOUT", ""


if __name__ == "__main__":
    sys.exit(main(S4PiiRecallZhtw))
