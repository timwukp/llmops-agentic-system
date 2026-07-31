"""finops-reconcile Lambda — drives the llmops_finops harness on a schedule.

Thin by design, like resume_pipeline: it resolves the harness, builds the payload,
delegates the streaming turn loop to harness_driver (whose stream-salvage, re-ask,
and self-reinvoke behaviour was earned from real production failures and should not
be reimplemented here), then records the outcome.

Why this is a separate Lambda rather than a state-machine stage: reconciliation
cannot run inside a run's lifetime. Cost Explorer lags roughly 24 hours and flags
recent periods ``Estimated: true``, so the bill for a run that finished yesterday
does not exist until today — by which point the run has no live agent and no task
token to settle. Reconciliation also spans MANY runs and answers to the project, not
to a run. So it is scheduled (EventBridge Scheduler, daily 09:00 UTC) and idempotent:
re-running a period is normal and expected, because a provisional period must be
re-read once it settles.

The three tasks:
  reconcile        — resource-level Cost Explorer + spans -> per-run actuals, variance
  pricing_refresh  — realized rates from our own bill, Price List only for gaps
  report           — project rollup + estimate-accuracy trend

Env: DATA_BUCKET, ESTIMATES_TABLE, ACTUALS_TABLE, DRIVER_FN,
     PROJECT (default llmops-agentic-system), LLMOPS_SNS_TOPIC.
"""
from __future__ import annotations

import datetime
import json
import os

import boto3
# Explicit import, matching resume_pipeline and the console: boto3.dynamodb is not
# auto-imported by ``import boto3``, so reaching it as an attribute raises
# AttributeError at the first query — a failure that only surfaces at runtime.
from boto3.dynamodb.conditions import Attr, Key

#: Cost Explorer marks the trailing ~24 h Estimated, and a period read too early
#: reports zero groups (verified live: a same-day resource-level query returned
#: Estimated=true with an empty group list). Reconciling D-2 by default means the
#: usual daily run reads a period that has actually landed, while an explicit
#: ``period`` in the event can still ask for anything.
DEFAULT_LAG_DAYS = 2

#: Re-reading a period is the point, not a bug: yesterday's provisional number is
#: expected to move. This bounds how far back a scheduled run looks for periods that
#: were provisional when first read.
RESETTLE_WINDOW_DAYS = 5

TASKS = ("reconcile", "pricing_refresh", "report")


def _clients():
    region = os.environ.get("AWS_REGION", "us-east-1")
    return {
        "lambda": boto3.client("lambda", region_name=region),
        "ddb": boto3.resource("dynamodb", region_name=region),
        "sns": boto3.client("sns", region_name=region),
    }


def default_period(today: datetime.date | None = None, lag_days: int = DEFAULT_LAG_DAYS) -> str:
    """The most recent day Cost Explorer has probably finished settling."""
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    return (today - datetime.timedelta(days=lag_days)).isoformat()


def unsettled_periods(ddb, project: str, today: datetime.date | None = None,
                      window_days: int = RESETTLE_WINDOW_DAYS) -> list[str]:
    """Periods already reconciled but still provisional, so they need re-reading.

    Without this, the first read of a period wins permanently and every number the
    dashboard shows for the last two days stays provisional forever — the settled
    figure exists in Cost Explorer but nothing ever goes back for it.
    """
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    table = ddb.Table(os.environ["ACTUALS_TABLE"])
    oldest = (today - datetime.timedelta(days=window_days)).isoformat()
    resp = table.query(
        KeyConditionExpression=Key("project").eq(project) & Key("sk").gte(oldest))
    periods = {str(i.get("sk", "")).split("#")[0]
               for i in resp.get("Items", [])
               if i.get("settlement") == "provisional"}
    return sorted(p for p in periods if p)


def runs_in_period(ddb, project: str, period: str) -> list[str]:
    """run_ids with an estimate on record — the reconcile targets for this period.

    A run with no estimate is still reconciled (its spend is real), but it is the
    estimate records that tell us what to compare against; the agent reports
    unestimated spend separately so the variance report can say honestly what
    fraction of spend was never estimated.
    """
    table = ddb.Table(os.environ["ESTIMATES_TABLE"])
    resp = table.scan(
        FilterExpression=Attr("project").eq(project),
        ProjectionExpression="id, run_id, #s, worst_case_usd, launched_at",
        ExpressionAttributeNames={"#s": "status"})
    out = []
    for item in resp.get("Items", []):
        rid = item.get("run_id")
        if rid and item.get("status") in ("launched", "reconciled"):
            out.append(str(rid))
    return sorted(set(out))


def build_payload(task: str, project: str, period: str, runs: list[str],
                  bucket: str, region: str, extra: dict | None = None) -> dict:
    """The harness invocation payload. Region and bucket travel in the payload —
    the agent is told never to hardcode account-specific values."""
    params = {"task": task, "project": project, "period": period, "runs": runs,
              "bucket": bucket, "region": region,
              "rates_uri": f"s3://{bucket}/finops/rates/rate_card_latest.json",
              "variance_threshold_pct": 20}
    params.update(extra or {})
    return {
        "run_id": f"finops-{period}",
        "stage": "finops",
        "task": task,
        "harness_id": "llmops_finops",
        "manifest_uri": f"s3://{bucket}/finops/manifests/{period}.json",
        "params": params,
    }


def record_outcome(ddb, project: str, period: str, task: str, result: dict) -> None:
    """One audit row per invocation, so a missing daily reconcile is visible.

    Keyed under a reserved ``#audit#`` sort key in the actuals table rather than a
    third table: it shares the (project, period) access pattern, and a run-log row
    can never collide with a cost row because no run_id contains '#'.
    """
    ddb.Table(os.environ["ACTUALS_TABLE"]).put_item(Item={
        "project": project,
        "sk": f"{period}#audit#{task}",
        "task": task,
        "status": str(result.get("status", "unknown")),
        "detail": json.dumps(result, default=str)[:8000],
    })


def handler(event, context=None, clients=None):
    """event: {} from the scheduler, or {task, period, project, runs} on demand."""
    c = clients or _clients()
    region = os.environ.get("AWS_REGION", "us-east-1")
    bucket = os.environ["DATA_BUCKET"]
    project = event.get("project") or os.environ.get("PROJECT", "llmops-agentic-system")

    task = event.get("task", "reconcile")
    if task not in TASKS:
        return {"error": f"unknown task {task!r}; expected one of {list(TASKS)}"}

    if event.get("period"):
        periods = [str(event["period"])]
    elif task == "reconcile":
        # The scheduled path: the newly-settled day, plus any earlier day still
        # provisional. Re-reading is intentional — see RESETTLE_WINDOW_DAYS.
        try:
            stale = unsettled_periods(c["ddb"], project)
        except Exception as exc:                     # a query failure must not skip today
            stale = []
            print(f"[finops] could not list provisional periods: {exc}")
        periods = sorted({default_period(), *stale})
    else:
        periods = [default_period()]

    results = []
    for period in periods:
        runs = event.get("runs")
        if runs is None and task == "reconcile":
            try:
                runs = runs_in_period(c["ddb"], project, period)
            except Exception as exc:
                # Attribution is by resource pattern, not by this list, so an empty
                # list degrades the variance comparison rather than the actuals.
                print(f"[finops] could not list runs for {period}: {exc}")
                runs = []
        payload = build_payload(task, project, period, runs or [], bucket, region,
                                extra=event.get("params"))
        resp = c["lambda"].invoke(
            FunctionName=os.environ["DRIVER_FN"],
            InvocationType="RequestResponse" if event.get("sync") else "Event",
            Payload=json.dumps(payload, default=str))
        outcome = {"period": period, "task": task,
                   "status": "invoked", "n_runs": len(runs or []),
                   "status_code": resp.get("StatusCode")}
        if event.get("sync"):
            raw = resp.get("Payload")
            body = raw.read() if hasattr(raw, "read") else raw
            try:
                outcome["driver"] = json.loads(body or "{}")
                outcome["status"] = outcome["driver"].get("status", "invoked")
            except (TypeError, ValueError):
                outcome["driver"] = {"_raw": str(body)[:500]}
        try:
            record_outcome(c["ddb"], project, period, task, outcome)
        except Exception as exc:
            print(f"[finops] could not record outcome for {period}: {exc}")
        results.append(outcome)

    return {"task": task, "project": project, "periods": periods,
            "results": results}
