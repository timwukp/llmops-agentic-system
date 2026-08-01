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


def seed_manifest(run_id: str, trigger_source: str, params: dict, plan: dict | None) -> dict:
    merged = {**DEFAULT_PARAMS, **(params or {})}
    return {
        "run_id": run_id,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "trigger_source": trigger_source,
        "iteration": 0,
        "models": {**DEFAULT_MODELS, **(params or {}).get("models", {})},
        "params": merged,
        "plan": plan or {},             # conductor-authored run plan, when present
        "stages": {},
    }


def handler(event, context=None, clients=None):
    """event: {trigger_source, params?, plan?} — from any trigger or the conductor."""
    c = clients or _clients()
    bucket = os.environ["DATA_BUCKET"]

    run_id = new_run_id()
    trigger_source = str(event.get("trigger_source", "unknown"))
    manifest = seed_manifest(run_id, trigger_source, event.get("params"), event.get("plan"))
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
        input=json.dumps({"run_id": run_id, "manifest_uri": manifest_uri,
                          "iteration": 0}))

    return {"run_id": run_id, "manifest_uri": manifest_uri,
            "execution_arn": execution["executionArn"]}
