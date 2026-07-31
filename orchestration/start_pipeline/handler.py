"""start-pipeline Lambda — single entry point for every trigger.

All four triggers (EventBridge Scheduler, GitHub Actions, Admin API, webhook)
and the conductor harness converge here. It mints the run_id, seeds the S3
manifest (the single source of truth every stage reads), records the run in
DynamoDB, emits PipelineStarted, and starts the Step Functions execution.

Env: DATA_BUCKET, RUNS_TABLE, EVENT_BUS, STATE_MACHINE_ARN.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import uuid

import boto3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))  # repo layout
try:
    from pipeline.contracts import events as ev
except ImportError:  # Lambda bundle layout
    import events as ev  # type: ignore

# Defaults are overridable per-run via the trigger payload's "params".
DEFAULT_MODELS = {
    "teacher": "us.deepseek.r1-v1:0",
    "student": "Qwen/Qwen3-1.7B",
}
DEFAULT_PARAMS = {
    "dataset": "arc-agi-2",
    "sample_count": 2000,
    "keep_reasoning": True,          # reasoning distillation for ARC domain
    "max_iterations": 3,             # remediation loop budget
    "training_instance": "ml.g5.2xlarge",
    "inference_instance": "ml.g5.xlarge",
    "gates": {"relative_solve_rate": 0.80, "format_validity": 0.95},
}

#: Sentinel for "this run was not dispatched from a conductor task".
#
# The state machine closes the conductor's llmops-tasks row when a run reaches a
# terminal state, which means it reads $.task_id -- and a JSONPath that is not present
# raises States.Runtime, which NO Catch can intercept (the run then dies before it can
# self-close, strictly worse than the zombie task being fixed). Most runs have no task:
# schedule and webhook triggers never went through a human plan approval. So the field
# is always set, and the closer's ConditionExpression makes this value a no-op write.
NO_TASK = "none"


def _clients():
    region = os.environ.get("AWS_REGION", "us-east-1")
    return {
        "s3": boto3.client("s3", region_name=region),
        "ddb": boto3.resource("dynamodb", region_name=region),
        "sfn": boto3.client("stepfunctions", region_name=region),
        "events": boto3.client("events", region_name=region),
    }


def new_run_id() -> str:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{stamp}-{uuid.uuid4().hex[:8]}"


def _as_obj(value, what: str) -> dict:
    """Accept an object or a JSON string; anything else is an error, loudly.

    The conductor's launch_run arguments are authored by a language model, and live it
    passed `params` as a JSON string. `{**DEFAULT_PARAMS, **params}` on a str raises
    "TypeError: 'str' object is not a mapping" -- start-pipeline 500s and the agent is
    told only "did not return a run_id", so an approved plan silently never dispatches.
    Coercing here (rather than tightening the prompt) fixes it for every caller.

    Unparseable input raises: running with defaults would spend GPU money on
    parameters no human approved."""
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{what} is a string but not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"{what} must be a JSON object, got {type(parsed).__name__}")
        return parsed
    raise ValueError(f"{what} must be an object or a JSON string, "
                     f"got {type(value).__name__}")


def seed_manifest(run_id: str, trigger_source: str, params, plan,
                  approval=None) -> dict:
    params = _as_obj(params, "params")
    plan = _as_obj(plan, "plan")
    approval = _as_obj(approval, "approval")
    merged = {**DEFAULT_PARAMS, **(params or {})}
    return {
        "run_id": run_id,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "trigger_source": trigger_source,
        "iteration": 0,
        "models": {**DEFAULT_MODELS, **_as_obj(params.get("models"), "params.models")},
        "params": merged,
        "plan": plan or {},             # conductor-authored run plan, when present
        # The signed human-acceptance record, stored verbatim like the plan: a run
        # must carry its own proof of who approved it (and the budget_usd ceiling
        # the monitor stage will one day enforce). Absent for non-conductor runs.
        "approval": approval or {},
        "stages": {},
    }


def handler(event, context=None, clients=None):
    """event: {trigger_source, params?, plan?, approval?} — from any trigger or the conductor."""
    c = clients or _clients()
    bucket = os.environ["DATA_BUCKET"]

    run_id = new_run_id()
    trigger_source = str(event.get("trigger_source", "unknown"))
    manifest = seed_manifest(run_id, trigger_source, event.get("params"), event.get("plan"),
                             event.get("approval"))
    manifest_uri = f"s3://{bucket}/runs/{run_id}/manifest.json"

    c["s3"].put_object(
        Bucket=bucket, Key=f"runs/{run_id}/manifest.json",
        Body=json.dumps(manifest, indent=2, default=str).encode(),
        ContentType="application/json")

    c["ddb"].Table(os.environ["RUNS_TABLE"]).put_item(Item={
        "run_id": run_id,
        "status": "running",
        "created_at": manifest["created_at"],
        "trigger_source": trigger_source,
        "iteration": 0,
    })

    ev.emit_event(os.environ["EVENT_BUS"], ev.PIPELINE_STARTED,
                  {"run_id": run_id, "trigger_source": trigger_source},
                  client=c["events"])

    execution = c["sfn"].start_execution(
        stateMachineArn=os.environ["STATE_MACHINE_ARN"],
        name=run_id,
        # pipeline_mode rides in the execution input because the Choice state at
        # the top of the machine cannot read the manifest from S3 — "full" runs
        # every stage; "data_audit" is the conductor's cheap starter (audit the
        # customer's data, report, stop before any GPU is provisioned).
        input=json.dumps({"run_id": run_id, "manifest_uri": manifest_uri,
                          "iteration": 0,
                          "pipeline_mode": manifest["params"].get("pipeline_mode", "full"),
                          # The conductor task this run answers to, so the machine can
                          # close that task out when the run ends -- it cannot read the
                          # manifest from S3, same constraint as pipeline_mode above.
                          "task_id": manifest["approval"].get("task_id") or NO_TASK}))

    return {"run_id": run_id, "manifest_uri": manifest_uri,
            "execution_arn": execution["executionArn"]}
