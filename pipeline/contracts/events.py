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
#: A model was hot-swapped to its fallback after a vendor 5xx burst. The pipeline is
#: CONTINUING; nobody needs to decide anything. This was emitted as
#: ESCALATED_TO_HUMAN with the word "informational" buried in a reason string, which
#: was harmless only for as long as nothing subscribed to EscalatedToHuman. The moment
#: a rule routed that detail-type to the conductor for triage, a self-healed run would
#: have paged the conductor -- so the discrimination has to live in the detail-type,
#: where a rule can see it, not in prose a rule cannot read.
MODEL_FAILED_OVER = "ModelFailedOver"
#: The conductor reached the end of its own authority and paged the owner. Distinct
#: from ESCALATED_TO_HUMAN on purpose: that one MEANS "a conductor should look at
#: this", and a page is what a conductor emits when it already has. Sharing one
#: detail-type made the triage rule feed itself -- escalate -> triage -> page ->
#: triage -- with each lap costing a real harness invocation.
OWNER_PAGED = "OwnerPaged"
#: A tracked SageMaker job was stopped with $0 billed — a capacity race loser or a
#: give-up on a Pending quota wait, not a code failure. Informational, like
#: MODEL_FAILED_OVER: the pipeline relaunches the stage without spending a remediation
#: iteration, and this event is how the timeline says why the same state ran twice.
CAPACITY_STOPPED = "CapacityStopped"
#: The resurrector re-invoked a driver whose heartbeat went silent mid-stage.
#: Informational, like MODEL_FAILED_OVER — the run continues; this is the timeline's
#: answer to "why did a fresh session appear an hour into a stage". A resurrection
#: that keeps recurring for one run is the signal to read, and the resurrector
#: escalates by itself past its cap.
DRIVER_RESURRECTED = "DriverResurrected"
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
    MODEL_FAILED_OVER,
    OWNER_PAGED,
    CAPACITY_STOPPED,
    DRIVER_RESURRECTED,
    PIPELINE_COMPLETED,
    PIPELINE_FAILED,
)

#: The detail-types that MUST have an EventBridge rule on the bus, and the reason each
#: one needs a listener. A detail-type absent from here is fire-and-forget by decision:
#: emitted for the audit trail, the console timeline and SNS, with nothing scheduled off
#: it. That decision is recorded HERE rather than inferred from the rules that happen to
#: exist, because "no rule" and "rule missing" look identical on a live bus -- the
#: llmops-pipeline bus carried ZERO rules for five phases while EscalatedToHuman was
#: emitted from three places and documented as routing to the conductor.
#:
#: deploy/07_lambdas.py builds a rule for each key; a test asserts the two sets match, so
#: adding an entry here without a rule fails offline instead of on the bus.
EVENTS_NEEDING_A_RULE: dict = {
    ESCALATED_TO_HUMAN: (
        "routes to the conductor for first-line triage (task='triage'); without a rule "
        "an escalated run waits for a human who was never told"),
}

#: For each detail-type an EventBridge rule may deliver straight to a Lambda, the name of
#: the function in that Lambda's handler that translates the bus envelope into the
#: payload the handler actually runs on.
#:
#: An EventBridge delivery is NOT a state-machine payload: it arrives wrapped in
#: {source, detail-type, detail, ...}, so a handler keyed on event["run_id"] raises
#: KeyError on the very first line. That is not hypothetical -- the driver was deployed
#: from a branch that lacked triage_event_from_bus while llmops-escalation-triage was
#: ENABLED and targeting it, and every escalation on the live bus died that way.
#:
#: Declared HERE, next to the detail-types, so deploy/07_lambdas.py can check the bytes it
#: is about to ship against the rules that are LIVE. The offline guards cannot: they
#: compare this tree's declarations against this tree's deployer, and a branch missing the
#: declaration AND the rule AND the translator is self-consistent and green. Only the bus
#: knows which rules exist.
BUS_DELIVERY_TRANSLATORS: dict = {
    ESCALATED_TO_HUMAN: "triage_event_from_bus",
}

#: Event source used on the custom bus.
EVENT_SOURCE = "llmops.pipeline"


class _Unset:
    """Sentinel so an OMITTED client can be told apart from an explicit None."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unset>"


_UNSET = _Unset()


def emit_event(
    bus: str,
    detail_type: str,
    detail: dict,
    client: Optional[Any] = _UNSET,
    source: str = EVENT_SOURCE,
) -> dict:
    """Put a single event on the given EventBridge bus.

    ``client`` is injectable for tests; when the argument is OMITTED a fresh boto3
    client is created (region resolved from the Lambda environment).

    Passing ``client=None`` explicitly is rejected rather than treated as omission.
    A None where a client belongs is a caller bug -- typically a test fake dict with
    ``"events": None`` for a client the author believed was never reached. Quietly
    substituting a production client there is the most expensive possible answer: on
    a developer machine with credentials the PutEvents SUCCEEDS, so the test passes
    while writing to the real bus, and the mistake shows up only in CI as a
    NoCredentialsError deep in botocore that names neither the test nor the bus.
    """
    if detail_type not in ALL_EVENTS:
        raise ValueError(f"Unknown detail-type {detail_type!r}; add it to events.py first")
    if client is None:
        raise ValueError(
            "emit_event got client=None. Pass a real or fake EventBridge client, or "
            "omit the argument entirely to resolve one from the environment. An "
            "explicit None is almost always an unstubbed client in a test fake.")
    if client is _UNSET:  # pragma: no cover - real AWS path
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
