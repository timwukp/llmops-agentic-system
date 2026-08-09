"""resurrector Lambda — re-invokes the harness driver for runs whose heartbeat died.

The gap this closes, from the incident that proved it exists: the driver hands a turn
to its next invocation with an ASYNC self-invoke (fire-and-forget). On 2026-08-08
Lambda dropped one such event (AsyncEventsDropped=1) for run-20260808T154900Z-68cfa9c8;
the AgentCore session idled out 15 minutes later and the run sat dead for NINE HOURS at
4/55 tasks — Step Functions still RUNNING, token still parked, money safe, and nothing
anywhere whose job it was to notice. An operator resurrected it by hand from the
execution history. The same gap swallows every other way a driver invocation can die
without reporting (Lambda timeout on the last turn, OOM, a crash after the token check),
and it is also what makes AgentCore's 8-hour session maxLifetime survivable: sessions
MAY die — all state lives in S3 and the session id is deterministic — provided something
re-invokes the driver afterward. This is that something.

Contract with the driver: every turn stamps the run row with `driver_beat_at` and
`driver_beat_payload` (the exact re-invoke payload, task token included). This Lambda
runs on a schedule and re-invokes the driver for any run that is
    status == "running"  AND  beat older than STALE_MINUTES  AND  no parked task_token.
The token check is what keeps it honest with launch-and-release: a run row holding
`task_token` is WAITING on a SageMaker job by design (resume_pipeline owns that wake),
not dead — resurrecting it would start a duplicate agent session next to a healthy wait.

Idempotency: the resurrection is conditional on the beat NOT having advanced since it
was read (ConditionExpression on driver_beat_at), so two overlapping sweeps cannot
double-resurrect; and the re-invoke itself is the driver's own continuation contract —
a fresh session re-reads manifest + progress from S3, which the 9-hour resurrection
proved loses nothing.

RESURRECTIONS_MAX caps how many times one run can be revived (default 5): a run whose
driver dies every turn has a real defect that revival only re-runs; past the cap this
Lambda emits ESCALATED_TO_HUMAN instead, which routes to the conductor's triage.

Env: RUNS_TABLE, DRIVER_FN, EVENT_BUS, STALE_MINUTES (default 20), RESURRECTIONS_MAX
(default 5).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import boto3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))  # repo layout
try:
    from pipeline.contracts import events as ev
except ImportError:  # Lambda bundle layout
    import events as ev  # type: ignore

STALE_MINUTES_DEFAULT = 20
RESURRECTIONS_MAX_DEFAULT = 5


def _clients():
    region = os.environ.get("AWS_REGION", "us-east-1")
    return {
        "ddb": boto3.resource("dynamodb", region_name=region),
        "lambda": boto3.client("lambda", region_name=region),
        "events": boto3.client("events", region_name=region),
    }


def _age_minutes(iso: str, now: datetime) -> float:
    try:
        return (now - datetime.fromisoformat(iso)).total_seconds() / 60
    except Exception:  # unparsable beat = treat as infinitely stale
        return float("inf")


def handler(event, context=None, clients=None):
    c = clients or _clients()
    table = c["ddb"].Table(os.environ["RUNS_TABLE"])
    stale_min = float(os.environ.get("STALE_MINUTES", STALE_MINUTES_DEFAULT))
    cap = int(os.environ.get("RESURRECTIONS_MAX", RESURRECTIONS_MAX_DEFAULT))
    now = datetime.now(timezone.utc)

    # Scan, not Query: runs have no status GSI and the table holds tens of rows.
    # The moment it holds thousands, add the index — this filter is the reminder.
    resp = table.scan()
    rows = resp.get("Items", [])
    while resp.get("LastEvaluatedKey"):
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        rows.extend(resp.get("Items", []))

    checked, out = 0, []
    for run in rows:
        if str(run.get("status")) != "running":
            continue
        checked += 1
        beat = str(run.get("driver_beat_at") or "")
        payload_raw = str(run.get("driver_beat_payload") or "")
        if not beat or not payload_raw:
            continue  # pre-heartbeat run (or non-driver run): nothing to act on
        if str(run.get("task_token") or ""):
            continue  # parked on a SageMaker job — resume_pipeline owns that wake
        age = _age_minutes(beat, now)
        if age < stale_min:
            continue

        run_id = str(run["run_id"])
        n = int(run.get("resurrections") or 0)
        if n >= cap:
            ev.emit_event(os.environ["EVENT_BUS"], ev.ESCALATED_TO_HUMAN,
                          {"run_id": run_id, "stage": "resurrector",
                           "reason": f"driver heartbeat dead {age:.0f}min; "
                                     f"resurrection cap ({cap}) exhausted — the driver "
                                     "dies every turn, revival only re-runs the defect"},
                          client=c["events"])
            out.append({"run_id": run_id, "action": "escalated", "age_min": round(age)})
            continue

        # Claim before invoking, conditional on the beat we read: if the driver came
        # back on its own (or another sweep got here first), the condition fails and
        # this sweep walks away — never two resurrections for one silence.
        try:
            table.update_item(
                Key={"run_id": run_id},
                UpdateExpression="SET resurrections = :n, driver_beat_at = :t",
                ConditionExpression="driver_beat_at = :seen",
                ExpressionAttributeValues={":n": n + 1,
                                           ":t": now.isoformat(),
                                           ":seen": beat})
        except Exception:  # ConditionalCheckFailed — someone else moved first
            out.append({"run_id": run_id, "action": "lost-claim"})
            continue

        c["lambda"].invoke(FunctionName=os.environ["DRIVER_FN"],
                           InvocationType="Event",
                           Payload=payload_raw.encode())
        ev.emit_event(os.environ["EVENT_BUS"], ev.DRIVER_RESURRECTED,
                      {"run_id": run_id, "stage": "resurrector",
                       "silent_minutes": round(age), "resurrection": n + 1},
                      client=c["events"])
        out.append({"run_id": run_id, "action": "resurrected",
                    "age_min": round(age), "n": n + 1})

    return {"checked_running": checked, "acted": out}
