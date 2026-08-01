"""Event vocabulary for the llmops-pipeline EventBridge bus.

Single source of truth for every detail-type the pipeline may emit. Orchestration
Lambdas import these constants instead of spelling strings inline, and the test
suite asserts that every detail-type referenced by the harness driver's
STAGE_EVENT_MAP exists here — an event name typo becomes a test failure, not a
silent rule that never matches.

Only stdlib + boto3 (Lambda-safe, no external deps).
"""
from __future__ import annotations

import datetime
import json
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Detail-types (chronological order of a happy-path run)
# ---------------------------------------------------------------------------
PIPELINE_STARTED = "PipelineStarted"
DATASET_GENERATED = "DatasetGenerated"
DATASET_CURATED = "DatasetCurated"
TRAINING_STARTED = "TrainingStarted"
MODEL_TRAINED = "ModelTrained"
MODEL_EVALUATED = "ModelEvaluated"
QUALITY_GATE_PASSED = "QualityGatePassed"
QUALITY_GATE_FAILED = "QualityGateFailed"
MODEL_REGISTERED = "ModelRegistered"
MODEL_DEPLOYED = "ModelDeployed"
SMOKE_TEST_PASSED = "SmokeTestPassed"
ENDPOINT_DELETED = "EndpointDeleted"
DRIFT_DETECTED = "DriftDetected"
ESCALATED_TO_HUMAN = "EscalatedToHuman"
PIPELINE_COMPLETED = "PipelineCompleted"
PIPELINE_FAILED = "PipelineFailed"

#: Every detail-type the pipeline is allowed to emit.
ALL_EVENTS: tuple = (
    PIPELINE_STARTED,
    DATASET_GENERATED,
    DATASET_CURATED,
    TRAINING_STARTED,
    MODEL_TRAINED,
    MODEL_EVALUATED,
    QUALITY_GATE_PASSED,
    QUALITY_GATE_FAILED,
    MODEL_REGISTERED,
    MODEL_DEPLOYED,
    SMOKE_TEST_PASSED,
    ENDPOINT_DELETED,
    DRIFT_DETECTED,
    ESCALATED_TO_HUMAN,
    PIPELINE_COMPLETED,
    PIPELINE_FAILED,
)

#: Event source used on the custom bus.
EVENT_SOURCE = "llmops.pipeline"


def emit_event(
    bus: str,
    detail_type: str,
    detail: dict,
    client: Optional[Any] = None,
    source: str = EVENT_SOURCE,
) -> dict:
    """Put a single event on the given EventBridge bus.

    ``client`` is injectable for tests; when omitted a fresh boto3 client is
    created (region resolved from the Lambda environment).
    """
    if detail_type not in ALL_EVENTS:
        raise ValueError(f"Unknown detail-type {detail_type!r}; add it to events.py first")
    if client is None:  # pragma: no cover - real AWS path
        import boto3

        client = boto3.client("events")
    detail = dict(detail)
    detail.setdefault("emitted_at", datetime.datetime.now(datetime.timezone.utc).isoformat())
    return client.put_events(
        Entries=[
            {
                "Source": source,
                "DetailType": detail_type,
                "Detail": json.dumps(detail, default=str),
                "EventBusName": bus,
            }
        ]
    )
