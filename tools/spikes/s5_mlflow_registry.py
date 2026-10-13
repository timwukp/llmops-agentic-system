#!/usr/bin/env python3
"""S5 — MLflow App -> SageMaker Model Registry sync and approval semantics.

Does register_model on an MLflow App auto-create a Model Package at PendingManualApproval? What
does a lifecycle alias actually change? Is an EventBridge 'SageMaker Model Package State Change'
event emitted on approval? Does a sagemaker:ModelLifeCycle / ModelApprovalStatus condition key
deny approval to a test role? Do CloudTrail CallMlflowApp data events appear?

Gates: Phase 2: registry-gated deploy.

Definition only: the question, the declared pass criteria and the planned resources. run() is
written and exercised live in this spike's own PR, in critical-path order (S7 -> S6 -> S4 first).
See docs/research/LAKEHOUSE_LLM_LIFECYCLE.md §10 and §13.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _spike import Spike, main  # noqa: E402


class S5MlflowRegistry(Spike):
    NAME = "s5_mlflow_registry"
    NUMBER = 5
    QUESTION = 'MLflow App -> SageMaker Model Registry sync and approval semantics'
    GATES = 'Phase 2: registry-gated deploy'
    PASS_CRITERIA = (
        ('auto_package', 'register_model creates a Model Package at PendingManualApproval'),
        ('alias', 'The effect of each sagemakerlifecycle-* alias is recorded'),
        ('event', 'An approval emits a Model Package State Change event'),
        ('deny', 'A condition key denies approval to the test role (simulate_principal_policy and a real call)'),
        ('cloudtrail', 'CallMlflowApp data events are recorded'),
    )
    IMPLEMENTED = False

    def plan(self) -> list[dict]:
        return [
            {"kind": 'mlflow app', "name": 'llmops-spike-s5'},
            {"kind": 'model package group', "name": 'llmops-spike-s5'},
            {"kind": 'iam role', "name": 'llmops-spike-s5-deny-test'},
            {"kind": 'eventbridge rule', "name": 'llmops-spike-s5 -> log group'},
        ]


if __name__ == "__main__":
    sys.exit(main(S5MlflowRegistry))
