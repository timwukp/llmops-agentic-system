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

# The two ways Step Functions says "this token is dead": TaskTimedOut carries the
# message 'Provided task does not exist anymore', TaskDoesNotExist is the never-existed
# case. Matched by botocore error CODE rather than by exception class, because the
# classes hang off a live client instance -- referencing sfn.exceptions.TaskTimedOut
# would make this module unimportable under an injected test double, and catching
# Exception here would swallow the throttles and 5xx that genuinely must be retried.
TASK_GONE_CODES = ("TaskTimedOut", "TaskDoesNotExist")


def _is_task_gone(exc) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    return code in TASK_GONE_CODES


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

    # A token Step Functions has already discarded is STALE DATA, not a pending
    # obligation -- so the settle call is isolated from the clear below, the same way
    # the driver isolates its canonical-report write from its own token settle. This
    # ordering bought a token that outlived its execution by three days: on 2026-07-29
    # this Lambda had no dynamodb:UpdateItem (AccessDenied at 11:06:54, since fixed),
    # the retry then found the task already gone (TaskTimedOut, "Provided task does not
    # exist anymore", ~5 deliveries through 11:10:14), and every retry raised BEFORE
    # reaching the clear. run-20260729T104648Z-41631739 still held a task_token for an
    # execution that ended 11:19:55Z. Reraising is right -- a settle that genuinely did
    # not land must be retried -- but it must not be the reason the field survives.
    settle_error = None
    try:
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
    except Exception as exc:  # noqa: BLE001 — re-raised below unless the token is dead
        if not _is_task_gone(exc):
            # A throttle, a 5xx, a bad token: the settle may still be achievable, so let
            # EventBridge retry. The clear is deliberately skipped on this path -- the
            # token is still the pipeline's only way to learn this stage finished.
            raise
        # The execution is over (timed out, aborted, or settled by another route), so the
        # token is dead whatever we do and the only outstanding work is clearing it.
        # Reraising would make EventBridge retry a call that can never succeed, and every
        # retry would leave the field exactly as it found it.
        settle_error = f"{type(exc).__name__}: {exc}"
        outcome = "token-already-gone"
        print(f"[resume] token for {run_id} was already gone: {settle_error}")

    # Clear the token so a duplicate EventBridge delivery can't double-settle, and so a
    # dead token does not sit in the row looking like work still in flight.
    try:
        c["ddb"].Table(os.environ["RUNS_TABLE"]).update_item(
            Key={"run_id": run_id},
            UpdateExpression="REMOVE task_token SET last_job_status = :s",
            ExpressionAttributeValues={":s": status})
    except Exception as exc:  # noqa: BLE001
        # Say so instead of dying silently: this exact failure (AccessDenied) is what
        # stranded the token in the first place, and it was invisible because the
        # traceback that followed it was about the settle, not about this write.
        print(f"[resume] FAILED to clear task_token for {run_id}: "
              f"{type(exc).__name__}: {exc}")
        raise

    return {"outcome": outcome, "run_id": run_id, "job_name": job_name,
            **({"settle_error": settle_error} if settle_error else {})}
