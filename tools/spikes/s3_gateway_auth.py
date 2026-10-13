#!/usr/bin/env python3
"""S3 — AgentCore Gateway vs API Gateway + Lambda in front of the SageMaker OpenAI path.

Which front door works for tenants calling the SageMaker OpenAI-compatible path: an AgentCore
Gateway inference-provider target, or an HTTP API with a JWT authorizer and a proxy Lambda that
mints a short-lived bearer token per request? Shares S2's endpoint.

Gates: Phase 2: gateway choice.

Definition only: the question, the declared pass criteria and the planned resources. run() is
written and exercised live in this spike's own PR, in critical-path order (S7 -> S6 -> S4 first).
See docs/research/LAKEHOUSE_LLM_LIFECYCLE.md §10 and §13.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _spike import Spike, main  # noqa: E402


class S3GatewayAuth(Spike):
    NAME = "s3_gateway_auth"
    NUMBER = 3
    QUESTION = 'AgentCore Gateway vs API Gateway + Lambda in front of the SageMaker OpenAI path'
    GATES = 'Phase 2: gateway choice'
    PASS_CRITERIA = (
        ('stream', "A tenant JWT gets a streamed completion (both paths, or one with the other's failure documented)"),
        ('tpm', 'A per-tenant RPM/TPM limit is enforced'),
        ('guardrail', 'ApplyGuardrail can be called inline'),
        ('latency', 'Added p50 latency is under 50 ms'),
        ('no_long_token', 'Neither path needs a long-lived token'),
    )
    IMPLEMENTED = False

    def plan(self) -> list[dict]:
        return [
            {"kind": 'cognito user pool', "name": 'llmops-spike-s3 (2 test tenants)'},
            {"kind": 'agentcore gateway', "name": 'llmops-spike-s3'},
            {"kind": 'http api + lambda', "name": 'llmops-spike-s3-proxy'},
        ]


if __name__ == "__main__":
    sys.exit(main(S3GatewayAuth))
