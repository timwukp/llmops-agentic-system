#!/usr/bin/env python3
"""S4 — PII detection recall on Traditional Chinese and English text.

Which detector, or which layering, catches PII in zh-TW and EN customer-service text? Compared on
1,000 seeded synthetic records with planted Taiwanese national IDs (checksum-valid), mobiles,
landlines, emails, business tax ids, card fragments with separators, names and addresses:
Glue Detect-PII, a Bedrock guardrail (ANONYMIZE + custom regexes), and the open-source
privacy-filter model run from the models-mirror at a pinned revision. Aggregates only are
committed; the records are never committed.

Gates: Phase 1 PR-07: the PII layering in Bronze -> Silver.

Definition only: the question, the declared pass criteria and the planned resources. run() is
written and exercised live in this spike's own PR, in critical-path order (S7 -> S6 -> S4 first).
See docs/research/LAKEHOUSE_LLM_LIFECYCLE.md §10 and §13.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _spike import Spike, main  # noqa: E402


class S4PiiRecallZhtw(Spike):
    NAME = "s4_pii_recall_zhtw"
    NUMBER = 4
    QUESTION = 'PII detection recall on Traditional Chinese and English text'
    GATES = 'Phase 1 PR-07: the PII layering in Bronze -> Silver'
    PASS_CRITERIA = (
        ('structured', 'A layered combination reaches recall >= 0.99 on structured types'),
        ('names', 'Recall >= 0.95 on Chinese and English names'),
        ('precision', 'Precision >= 0.9'),
        ('over_redaction', 'The over-redaction rate is measured per detector'),
    )
    IMPLEMENTED = False

    def plan(self) -> list[dict]:
        return [
            {"kind": 'glue job', "name": 'llmops-spike-s4-detect-pii (Glue 5)'},
            {"kind": 'bedrock guardrail', "name": 'llmops-spike-pii'},
            {"kind": 'processing job', "name": 'llmops-spike-s4-privacy-filter'},
            {"kind": 's3 objects', "name": 's3://<DATA_BUCKET>/spikes/s4/'},
        ]


if __name__ == "__main__":
    sys.exit(main(S4PiiRecallZhtw))
