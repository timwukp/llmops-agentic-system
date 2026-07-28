"""resume-pipeline Lambda — settles the parked task token when training finishes.

The launch-and-release contract: the finetune harness launches the SageMaker
training job and calls the job_launched inline function; the harness driver
parks the Step Functions task token in DynamoDB keyed by run_id and records
job_name. An EventBridge rule on "SageMaker Training Job State Change"
(Completed | Failed | Stopped) invokes this Lambda, which looks the run up by
job name (GSI job_name-index), emits ModelTrained / PipelineFailed, and
settles the token so the state machine moves to eval.

Env: RUNS_TABLE, EVENT_BUS.
"""
from __future__ import annotations

import json
import os
import sys

import boto3
from boto3.dynamodb.conditions import Key

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))  # repo layout
try:
    from pipeline.contracts import events as ev
except ImportError:  # Lambda bundle layout
    import events as ev  # type: ignore

TERMINAL_OK = ("Completed",)
TERMINAL_BAD = ("Failed", "Stopped")


def _clients():
    region = os.environ.get("AWS_REGION", "us-east-1")
    return {
        "ddb": boto3.resource("dynamodb", region_name=region),
        "sfn": boto3.client("stepfunctions", region_name=region),
        "events": boto3.client("events", region_name=region),
    }


def find_run_by_job(ddb, job_name: str) -> dict | None:
    table = ddb.Table(os.environ["RUNS_TABLE"])
    resp = table.query(IndexName="job_name-index",
                       KeyConditionExpression=Key("job_name").eq(job_name))
    items = resp.get("Items", [])
    return items[0] if items else None


def handler(event, context=None, clients=None):
    """event: the EventBridge SageMaker Training Job State Change envelope."""
    c = clients or _clients()
    detail = event.get("detail", {})
    job_name = detail.get("TrainingJobName", "")
    status = detail.get("TrainingJobStatus", "")

    if status not in TERMINAL_OK + TERMINAL_BAD:
        return {"skipped": True, "reason": f"non-terminal status {status!r}"}

    run = find_run_by_job(c["ddb"], job_name)
    if not run:
        # Not one of ours (other jobs in the account fire the same rule).
        return {"skipped": True, "reason": f"no run tracks job {job_name!r}"}

    run_id = run["run_id"]
    token = run.get("task_token", "")
    if not token:
        return {"skipped": True, "reason": f"run {run_id} has no parked token"}

    if status in TERMINAL_OK:
        model_uri = (detail.get("ModelArtifacts", {}) or {}).get("S3ModelArtifacts", "")
        ev.emit_event(os.environ["EVENT_BUS"], ev.MODEL_TRAINED,
                      {"run_id": run_id, "job_name": job_name,
                       "model_artifacts": model_uri}, client=c["events"])
        c["sfn"].send_task_success(taskToken=token, output=json.dumps({
            "run_id": run_id, "job_name": job_name, "status": "trained",
            "model_artifacts": model_uri}))
        outcome = "resumed"
    else:
        reason = detail.get("FailureReason", status)
        ev.emit_event(os.environ["EVENT_BUS"], ev.PIPELINE_FAILED,
                      {"run_id": run_id, "job_name": job_name,
                       "reason": reason}, client=c["events"])
        c["sfn"].send_task_failure(taskToken=token,
                                   error="TrainingJobFailed", cause=str(reason)[:250])
        outcome = "failed"

    # Clear the token so a duplicate EventBridge delivery can't double-settle.
    c["ddb"].Table(os.environ["RUNS_TABLE"]).update_item(
        Key={"run_id": run_id},
        UpdateExpression="REMOVE task_token SET last_job_status = :s",
        ExpressionAttributeValues={":s": status})

    return {"outcome": outcome, "run_id": run_id, "job_name": job_name}
