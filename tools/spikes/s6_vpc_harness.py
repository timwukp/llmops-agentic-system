#!/usr/bin/env python3
"""S6 — VPC-mode AgentCore harness without NAT.

What does a harness need to run in VPC mode with no internet route? A clone of the data-prep
harness in VPC network mode, with its S3 skill sources and the existing execution role, is
checked for: session start, skill resolution, the model call, and shell access to s3tables,
Athena and Glue through interface endpoints. Records which tools egress and what fails
(mirror_model's Hugging Face pull is expected to need the public network).

Gates: Phase 1 PR-03: VPC foundation.

Definition only: the question, the declared pass criteria and the planned resources. run() is
written and exercised live in this spike's own PR, in critical-path order (S7 -> S6 -> S4 first).
See docs/research/LAKEHOUSE_LLM_LIFECYCLE.md §10 and §13.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _spike import Spike, main  # noqa: E402


class S6VpcHarness(Spike):
    NAME = "s6_vpc_harness"
    NUMBER = 6
    QUESTION = 'VPC-mode AgentCore harness without NAT'
    GATES = 'Phase 1 PR-03: VPC foundation'
    PASS_CRITERIA = (
        ('session', 'A session starts and the S3 skill sources resolve'),
        ('model', 'The model call succeeds through the bedrock-runtime endpoint'),
        ('tools', 'aws s3tables list-table-buckets, an Athena query and glue get-job succeed from the shell'),
        ('egress', 'Which tools need egress is recorded'),
        ('failures', 'What fails without NAT is recorded'),
    )
    IMPLEMENTED = False

    def plan(self) -> list[dict]:
        return [
            {"kind": 'subnet', "name": 'temporary third subnet (3 AZs)'},
            {"kind": 'interface endpoints', "name": 'bedrock-runtime, bedrock-agentcore, sts, logs, ssm, s3tables, glue, athena, secretsmanager (+ecr if needed)'},
            {"kind": 'agentcore harness', "name": 'llmops_spike_vpc (VPC network mode)'},
        ]


if __name__ == "__main__":
    sys.exit(main(S6VpcHarness))
