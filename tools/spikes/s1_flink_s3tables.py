#!/usr/bin/env python3
"""S1 — Managed Flink -> S3 Tables via the Glue Iceberg REST catalog.

Can a Managed Service for Apache Flink app on Flink 1.20 (and the newest runtime offered)
write upserts -- Iceberg v2 equality deletes -- to an S3 Tables table through the Glue Iceberg
REST endpoint, both on the public network and with VpcConfig? The official AWS sample says
Flink 1.20 "isn't supported with the S3 Table Sink yet"; the REST route is undocumented.

Gates: Phase 3: Managed Flink as a core component (else Glue streaming/MERGE writes Silver).

Definition only: the question, the declared pass criteria and the planned resources. run() is
written and exercised live in this spike's own PR, in critical-path order (S7 -> S6 -> S4 first).
See docs/research/LAKEHOUSE_LLM_LIFECYCLE.md §10 and §13.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _spike import Spike, main  # noqa: E402


class S1FlinkS3Tables(Spike):
    NAME = "s1_flink_s3tables"
    NUMBER = 1
    QUESTION = 'Managed Flink -> S3 Tables via the Glue Iceberg REST catalog'
    GATES = 'Phase 3: Managed Flink as a core component (else Glue streaming/MERGE writes Silver)'
    PASS_CRITERIA = (
        ('count', 'Athena COUNT(*) equals the generated row count after N checkpoints'),
        ('upsert', 'An upserted key holds only its latest value'),
        ('restore', 'A restart from a snapshot produces no duplicates'),
        ('compaction', 'S3 Tables compaction runs on the table (GetTableMaintenanceJobStatus)'),
        ('vpc', 'The VPC variant works with only s3tables, glue and sts interface endpoints'),
    )
    IMPLEMENTED = False

    def plan(self) -> list[dict]:
        return [
            {"kind": 's3tables bucket', "name": 'llmops-spike-s1'},
            {"kind": 's3tables table', "name": 'llmops-spike-s1/spike/events (v2)'},
            {"kind": 'iam role', "name": 'llmops-spike-s1-flink'},
            {"kind": 's3 object', "name": 'app jar under s3://<DATA_BUCKET>/spikes/s1/ (sha256 recorded)'},
            {"kind": 'managed flink app', "name": 'llmops-spike-s1 (datagen source, run public then VPC)'},
        ]


if __name__ == "__main__":
    sys.exit(main(S1FlinkS3Tables))
