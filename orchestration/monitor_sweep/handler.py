"""monitor-sweep Lambda — drives the llmops_monitor harness's `sweep` task on a schedule.

Thin by design, exactly like finops_reconcile: it builds the payload, delegates the
streaming turn loop to harness_driver (whose stream-salvage, re-ask and self-reinvoke
behaviour was earned from real production failures and must not be reimplemented), and
records the outcome where a MISSED sweep is visible.

Why a schedule and not a state-machine stage — the same argument that put the auditor
outside the spine, applied to the one monitor task that shares its shape:

  * `health` is run-scoped: it reads CloudWatch for THIS run's endpoint, and only while
    that endpoint exists. It belongs between SmokeTest and Teardown, and that is where it
    now is.
  * `sweep` is the opposite on both axes. It looks for endpoints left running by OTHER
    runs, and the whole point is finding ones nobody is watching — including runs that
    already ended, whose executions are gone and whose task tokens are settled. A
    run-scoped agent cannot answer for other runs, and an orphan created by a run that
    crashed will never be found by a sweep that only ever runs inside a healthy run.
    That is precisely the endpoint that costs money for a month.

The account already proves the point: the only endpoint standing in it is
``jumpstart-dft-hf-asr-whisper-large-v2``, InService since 2024-04-11 and carrying no
``project`` tag at all. No run will ever be responsible for it. Something outside every
run has to look.

Env: DATA_BUCKET, DRIVER_FN, EVENTS_TABLE, PROJECT (default llmops-agentic-system),
     IDLE_HOURS (default 2, matching the prompt's threshold).

EVENTS_TABLE, not RUNS_TABLE: a sweep is not a run and must never appear as one. Writing
it to the runs table would put a synthetic ``sweep-<date>`` row alongside real runs, where
the console lists it, the finops auditor tries to reconcile its cost, and the run count
every doc quotes goes up by one per day.
"""
from __future__ import annotations

import datetime
import json
import os

import boto3

#: The prompt's own threshold ("flag any idle >2 hours"). Kept here as well as there so
#: the schedule and the agent cannot disagree about what "idle" means -- it travels in
#: the payload rather than being restated in prose the agent might round differently.
DEFAULT_IDLE_HOURS = 2

#: Only `sweep` is scheduled. `health` and `report` are run-scoped and live in the state
#: machine (MonitorHealth / MonitorReport); dispatching them from here would invent a
#: run_id for a run that does not exist and write into another run's prefix.
TASKS = ("sweep",)

#: Cross-run output prefix. NOT under runs/<run_id>/ on purpose: a sweep's findings are
#: about endpoints that outlived their runs, so filing them under one run's prefix would
#: bury the account-level answer inside whichever run happened to look.
SWEEP_PREFIX = "monitoring"


def _clients():
    region = os.environ.get("AWS_REGION", "us-east-1")
    return {
        "lambda": boto3.client("lambda", region_name=region),
        "ddb": boto3.resource("dynamodb", region_name=region),
        "sns": boto3.client("sns", region_name=region),
    }


def sweep_id(today: datetime.date | None = None) -> str:
    """The synthetic run_id for a sweep: ``sweep-<YYYY-MM-DD>``.

    A sweep has no run, but the driver keys its session id and every stage-event row off
    run_id, so it needs one. Date-derived rather than random so a re-run of the same day
    lands in the same session and the same rows -- re-running a sweep is normal, and two
    sweeps of one day must not read as two different findings.
    """
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    return f"sweep-{today.isoformat()}"


def build_payload(project: str, bucket: str, region: str, run_id: str,
                  idle_hours: int = DEFAULT_IDLE_HOURS, extra: dict | None = None) -> dict:
    """The harness invocation payload.

    ``project`` travels as the tag value the agent cross-checks against -- NOT as a
    filter it may trust. See the prompt: an endpoint with no ``project`` tag is
    unattributable, not foreign, and the single genuine orphan in this account is exactly
    that. Region and bucket travel in the payload because every agent is told never to
    hardcode account-specific values.
    """
    params = {
        "task": "sweep",
        "project": project,
        "bucket": bucket,
        "region": region,
        "idle_hours": idle_hours,
        "sweep_uri": f"s3://{bucket}/{SWEEP_PREFIX}/sweeps/{run_id}.json",
    }
    params.update(extra or {})
    return {
        "run_id": run_id,
        "stage": "monitor",
        "task": "sweep",
        "harness_id": "llmops_monitor",
        "manifest_uri": f"s3://{bucket}/{SWEEP_PREFIX}/manifests/{run_id}.json",
        "params": params,
    }


def record_outcome(ddb, run_id: str, result: dict) -> None:
    """One row per sweep in the stage-events table, so a sweep that never ran is visible.

    Same reasoning as finops_reconcile's reserved ``#audit#`` key: the failure mode worth
    engineering against is not a sweep that reports badly, it is a sweep that silently
    stopped happening. A cost control nobody can tell has stopped is not a control.
    """
    ddb.Table(os.environ["EVENTS_TABLE"]).put_item(Item={
        "run_id": run_id,
        "sk": f"sweep#{result.get('task', 'sweep')}",
        "stage": "monitor",
        "status": str(result.get("status", "unknown")),
        "detail": json.dumps(result, default=str)[:8000],
    })


def handler(event, context=None, clients=None):
    """event: {} from the scheduler, or {task, project, idle_hours, sync, params}."""
    c = clients or _clients()
    region = os.environ.get("AWS_REGION", "us-east-1")
    bucket = os.environ["DATA_BUCKET"]
    project = event.get("project") or os.environ.get("PROJECT", "llmops-agentic-system")

    task = event.get("task", "sweep")
    if task not in TASKS:
        return {"error": f"unknown task {task!r}; expected one of {list(TASKS)}"}

    try:
        idle_hours = int(event.get("idle_hours")
                         or os.environ.get("IDLE_HOURS", DEFAULT_IDLE_HOURS))
    except (TypeError, ValueError):
        idle_hours = DEFAULT_IDLE_HOURS

    run_id = str(event.get("run_id") or sweep_id())
    payload = build_payload(project, bucket, region, run_id, idle_hours,
                            extra=event.get("params"))

    resp = c["lambda"].invoke(
        FunctionName=os.environ["DRIVER_FN"],
        InvocationType="RequestResponse" if event.get("sync") else "Event",
        Payload=json.dumps(payload, default=str))

    outcome = {"run_id": run_id, "task": task, "status": "invoked",
               "idle_hours": idle_hours, "status_code": resp.get("StatusCode")}
    if event.get("sync"):
        raw = resp.get("Payload")
        body = raw.read() if hasattr(raw, "read") else raw
        try:
            outcome["driver"] = json.loads(body or "{}")
            outcome["status"] = outcome["driver"].get("status", "invoked")
        except (TypeError, ValueError):
            outcome["driver"] = {"_raw": str(body)[:500]}

    try:
        record_outcome(c["ddb"], run_id, outcome)
    except Exception as exc:  # noqa: BLE001 — a bookkeeping failure must not lose the sweep
        print(f"[monitor-sweep] could not record outcome for {run_id}: {exc}")

    return {"task": task, "project": project, "run_id": run_id, "result": outcome}
