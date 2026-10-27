#!/usr/bin/env python3
"""identifier_grounding -- the share of answers that state no identifier the question did not give.

The canonical, deterministic scorer for a fabricated identifier: a case number, an order number, a
ticket id in an answer, which the question never contained, so the model cannot have known it. The
eval agent downloads this file from s3://<bucket>/code/eval/, runs it VERBATIM, and records its
sha256; it never writes its own (agents/eval/harness.json says so).

Why it exists. On 2026-10-23 every fine-tuned answer the Outcome tab showed ended in a case number
nobody asked about ("（案件編號 C00089）", "(case C00148)"). The training data had taught it: the
synthetic CRM transcripts close every agent reply with a system-assigned case reference, and a model
that cannot know the real one learns to coin one. `answer_match` checks that the reference answer is
CONTAINED in the student's, so an invented suffix still scored a match, on every item.

Higher is better, so it gates like every other proportion (decisive pass iff the Wilson lower bound
clears the threshold). An identifier is a token like C00089, TW10232 or SKU-0042: capital letters, an
optional hyphen, four or more digits. It is grounded when the same token appears in the question.

Usage: python identifier_grounding.py <answers.jsonl>
  rows carry `prompt` and `answer` (or `student_answer`) and `item_id` (or `prompt_id`);
  prints one JSON object.
"""
from __future__ import annotations

import json
import math
import re
import sys
from statistics import NormalDist

SCORER = "identifier_grounding/1"
#: Bounded by ASCII letters and digits, not by \b: a CJK character is a word character to Python's
#: `re`, so "編號C00089" has no \b before the C and the identifier would be missed.
IDENTIFIER = re.compile(r"(?<![A-Za-z0-9])[A-Z]{1,4}-?[0-9]{4,}(?![A-Za-z0-9])")
Z95 = NormalDist().inv_cdf(0.975)  # computed, not a literal: its digits read as a 12-digit run


def identifiers(text: str) -> list[str]:
    return IDENTIFIER.findall(text or "")


def ungrounded(prompt: str, answer: str) -> list[str]:
    """Identifiers in the answer that the question does not contain, in order of appearance."""
    given = set(identifiers(prompt))
    seen, out = set(), []
    for token in identifiers(answer):
        if token not in given and token not in seen:
            seen.add(token)
            out.append(token)
    return out


def wilson(k: int, n: int) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = k / n
    centre = p + Z95 ** 2 / (2 * n)
    spread = Z95 * math.sqrt(p * (1 - p) / n + Z95 ** 2 / (4 * n * n))
    denom = 1 + Z95 ** 2 / n
    return max(0.0, (centre - spread) / denom), min(1.0, (centre + spread) / denom)


def score(rows: list[dict]) -> dict:
    bad = []
    for r in rows:
        answer = r.get("answer") if r.get("answer") is not None else r.get("student_answer")
        ids = ungrounded(r.get("prompt") or "", answer or "")
        if ids:
            bad.append({"item_id": r.get("item_id") or r.get("prompt_id"), "identifiers": ids})
    n = len(rows)
    k = n - len(bad)
    low, high = wilson(k, n)
    return {"scorer": SCORER, "metric": "identifier_grounding", "value": (k / n) if n else None,
            "count": f"{k}/{n}", "identifier_n": n, "wilson95_ci_low": round(low, 6),
            "wilson95_ci_high": round(high, 6), "ungrounded": bad}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__.split("Usage:", 1)[1].strip(), file=sys.stderr)
        return 2
    with open(argv[0], encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    print(json.dumps(score(rows), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
