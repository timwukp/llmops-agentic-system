"""harness-driver Lambda — the bridge between Step Functions and a worker harness.

One invocation = one harness task. Streams InvokeHarness, services the
inline-function protocol (toolUse ⇄ toolResult), verifies claimed outputs,
publishes canonical state, and settles the Step Functions task token.

Production patterns baked in (from Tim's live CI agents — real failures, not theory):
  - BotoConfig(read_timeout=870, retries=0): default 60s read timeout kills long
    streams; botocore auto-retry would silently re-run a whole agent turn.
  - safe stream loop: streams die mid-turn (urllib3 timeout, reset, runtimeClientError);
    salvage and retry the SAME session once before failing.
  - missing-signal re-ask: models narrate completion but skip the structured call;
    re-invoke the same session once demanding stage_complete.
  - empty-but-valid: stage_complete with outputs=[] is a legitimate success.
  - trust-but-verify: head_object every claimed S3 output; the driver (not the
    agent) writes the canonical report.

Env: RUNS_TABLE, EVENTS_TABLE, EVENT_BUS, LLMOPS_SNS_TOPIC, DATA_BUCKET.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from typing import Optional

import boto3
from botocore.config import Config as BotoConfig
from boto3.dynamodb.conditions import Key

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))  # repo layout
try:
    from pipeline.contracts import events as ev
    from pipeline.contracts.report import normalize_stage_complete, write_run_report
    from orchestration import conductor_tools
except ImportError:  # Lambda bundle layout (contracts vendored alongside)
    import events as ev  # type: ignore
    from report import normalize_stage_complete, write_run_report  # type: ignore
    import conductor_tools  # type: ignore

# (stage, task) -> EventBridge detail-type. eval/gate resolved dynamically.
STAGE_EVENT_MAP = {
    ("data-prep", "generate"): ev.DATASET_GENERATED,
    ("data-prep", "curate"): ev.DATASET_CURATED,
    ("finetune", "analyze"): ev.MODEL_TRAINED,
    # ModelEvaluated was declared in the event vocabulary and emitted by NOTHING --
    # the same absence as the evaluate task itself, from the other side.
    ("eval", "evaluate"): ev.MODEL_EVALUATED,
    ("deploy", "deploy"): ev.MODEL_DEPLOYED,
    ("deploy", "smoke"): ev.SMOKE_TEST_PASSED,
    ("deploy", "teardown"): ev.ENDPOINT_DELETED,
}

_AGENTCORE_CFG = BotoConfig(read_timeout=870, connect_timeout=30,
                            retries={"max_attempts": 0})

#: The conductor's own run_id for a triage. NOT the escalated run's id, which is the
#: obvious choice and is wrong twice over:
#:
#:   * take_directive() is keyed on event["run_id"], and the checkpoint branch is its
#:     only caller. A triage invoked under the subject's id would pop the subject's own
#:     parked verdict -- the one the conductor is about to write -- and hand it to the
#:     conductor as an instruction from a human. The conductor would then be answering
#:     itself.
#:   * handle_escalate() and handle_job_launched() update the runs table keyed on
#:     event["run_id"]. A triage that escalated in turn would overwrite the subject
#:     run's status and job_name with the triage's.
#:
#: The subject run reaches the conductor as params.escalation.run_id, which is what the
#: orchestrator prompt's triage clause already tells it to read.
TRIAGE_STAGE = "orchestrator"
TRIAGE_TASK = "triage"


def _is_triage(event) -> bool:
    return event.get("stage") == TRIAGE_STAGE and event.get("task") == TRIAGE_TASK


def triage_event_from_bus(record: dict, bucket: str) -> dict:
    """Turn an EventBridge EscalatedToHuman event into a driver invocation.

    Done HERE, in Python, rather than in an EventBridge InputTransformer, for one
    reason: a transformer that references a JSON path the event does not carry drops the
    event silently -- no invocation, no failure, nothing on any metric that names this
    pipeline. That is the exact class of defect this task exists to fix (an emitted
    event with no listener), and an InputTransformer would let it back in through a
    channel with even less visibility, since the ASL's own EscalateFail entry carries a
    different key set from the driver's handle_escalate entry. A dict built in a
    function is testable offline against every real emitter's payload.

    manifest_uri is derived from the SUBJECT run, not the triage: the conductor's job is
    to read the stuck run's manifest, and there is no manifest for a triage.
    """
    detail = record.get("detail") or {}
    subject = str(detail.get("run_id") or "")
    if not subject:
        # An escalation with no run_id names nothing to triage. Better to fail loudly
        # here than to invoke a conductor that will read an empty manifest URI.
        raise ValueError(f"EscalatedToHuman with no run_id in detail: {detail!r}")
    return {
        "run_id": f"triage-{subject}",
        "stage": TRIAGE_STAGE,
        "task": TRIAGE_TASK,
        "harness_id": "llmops_orchestrator",
        "manifest_uri": f"s3://{bucket}/runs/{subject}/manifest.json",
        "params": {"escalation": {
            "run_id": subject,
            "stage": detail.get("stage", ""),
            "reason": detail.get("reason", ""),
            "iteration": detail.get("iteration", 0),
            "escalated_at": detail.get("emitted_at", ""),
        }},
    }


def session_id(run_id: str, stage: str, task: str) -> str:
    """Deterministic, >=33 chars (AgentCore minimum). Same task -> same session."""
    base = f"{run_id}-{stage}-{task}"
    if len(base) >= 33:
        return base[:100]
    return (base + "-" + hashlib.sha256(base.encode()).hexdigest())[:64]


def _clients():
    region = os.environ.get("AWS_REGION", "us-east-1")
    return {
        "agentcore": boto3.client("bedrock-agentcore", region_name=region, config=_AGENTCORE_CFG),
        "ddb": boto3.resource("dynamodb", region_name=region),
        "s3": boto3.client("s3", region_name=region),
        "sfn": boto3.client("stepfunctions", region_name=region),
        "sns": boto3.client("sns", region_name=region),
        "events": boto3.client("events", region_name=region),
        "lambda": boto3.client("lambda", region_name=region),
    }


def _kms(c: dict):
    """KMS client for approval-signature verification, created on first use.

    Not in _clients() because only the launch_run path needs it, and the unit-test
    fake client dicts predate it — a lazy accessor means they keep working and a
    test can inject c["kms"] to stub verification.
    """
    if "kms" not in c:
        region = os.environ.get("AWS_REGION", "us-east-1")
        c["kms"] = boto3.client("kms", region_name=region)
    return c["kms"]


_arn_cache: dict = {}


def _resolve_harness_arn(harness_id: str) -> str:
    """InvokeHarness requires the full ARN (live-verified: harnessId is not an
    accepted parameter). The state machine passes logical names (llmops_data_prep);
    the full suffixed id lives in SSM /llmops/harness/<agent> (set by 05_harnesses.py).
    Env override HARNESS_ARN_<NAME> short-circuits SSM (also keeps unit tests offline)."""
    if harness_id.startswith("arn:"):
        return harness_id
    if harness_id in _arn_cache:
        return _arn_cache[harness_id]
    env_key = f"HARNESS_ARN_{harness_id.upper()}"
    if os.environ.get(env_key):
        _arn_cache[harness_id] = os.environ[env_key]
        return _arn_cache[harness_id]
    region = os.environ.get("AWS_REGION", "us-east-1")
    agent = harness_id.removeprefix("llmops_").replace("_", "-")
    ssm = boto3.client("ssm", region_name=region)
    full_id = ssm.get_parameter(Name=f"/llmops/harness/{agent}")["Parameter"]["Value"]
    account = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    _arn_cache[harness_id] = f"arn:aws:bedrock-agentcore:{region}:{account}:harness/{full_id}"
    return _arn_cache[harness_id]


def _invoke(ac, harness_id: str, sess: str, messages: list, qualifier: Optional[str]):
    """`messages` is the full messages list -- a resume needs two entries (assistant
    toolUse echo + user toolResult), so this can no longer wrap a single content
    block. See _tool_result_content."""
    kwargs = dict(harnessArn=_resolve_harness_arn(harness_id), runtimeSessionId=sess,
                  messages=messages)
    if qualifier:
        kwargs["qualifier"] = qualifier
    return ac.invoke_harness(**kwargs)


def _drain(resp) -> dict:
    """Consume the stream; return {text, tool_use, stop_reason, error}."""
    text, tool_use, stop_reason, error = [], None, None, None
    try:
        for event in resp.get("stream", []):
            if "contentBlockDelta" in event:
                delta = event["contentBlockDelta"].get("delta", {})
                if "text" in delta:
                    text.append(delta["text"])
            if "contentBlockStart" in event:
                start = event["contentBlockStart"].get("start", {})
                if "toolUse" in start:
                    tool_use = {"toolUseId": start["toolUse"].get("toolUseId"),
                                "name": start["toolUse"].get("name"), "input": ""}
            if tool_use is not None and "contentBlockDelta" in event:
                delta = event["contentBlockDelta"].get("delta", {})
                if "toolUse" in delta:
                    tool_use["input"] += delta["toolUse"].get("input", "")
            if "messageStop" in event:
                stop_reason = event["messageStop"].get("stopReason")
    except Exception as exc:  # stream death is expected in production
        error = f"{type(exc).__name__}: {exc}"
    if tool_use is not None and isinstance(tool_use.get("input"), str):
        try:
            tool_use["input"] = json.loads(tool_use["input"] or "{}")
        except json.JSONDecodeError:
            tool_use["input"] = {"_raw": tool_use["input"]}
    return {"text": "".join(text), "tool_use": tool_use,
            "stop_reason": stop_reason, "error": error}


def _user_text(text: str) -> list:
    """A plain user turn as a messages list."""
    return [{"role": "user", "content": [{"text": text}]}]


def _tool_result_content(tool_use: dict, payload: dict) -> list:
    """Resume a paused inline function: echo the toolUse, THEN answer it.

    Two messages, per the InvokeHarness pause/resume contract -- an assistant message
    replaying the emitted toolUse, then a user message with the matching toolResult.
    A lone toolResult is rejected ("The number of toolResult blocks at
    messages.N.content exceeds the number of toolUse blocks of previous turn"),
    because the history handed to the runtime contains no call to answer. Found live
    on the console's dispatch path, where it broke four sessions in a row; here the
    same bug hid behind the stream-retry, a rejected result looking like stream death.

    JSON travels in a TEXT block: `json` blocks come back as
    "runtimeClientError ... content_type=<json_> | unsupported type"."""
    return [
        {"role": "assistant", "content": [{"toolUse": {
            "toolUseId": tool_use["toolUseId"],
            "name": tool_use["name"],
            "input": tool_use.get("input") or {}}}]},
        {"role": "user", "content": [{"toolResult": {
            "toolUseId": tool_use["toolUseId"],
            "content": [{"text": json.dumps(payload, default=str)}],
            "status": "success"}}]},
    ]


def verify_outputs(s3, outputs: list) -> list:
    """head_object every claimed s3:// URI; return the missing ones."""
    missing = []
    for uri in outputs or []:
        if not uri.startswith("s3://"):
            continue
        bucket, _, key = uri[5:].partition("/")
        try:
            s3.head_object(Bucket=bucket, Key=key)
        except Exception:
            missing.append(uri)
    return missing


def _load_manifest(s3, manifest_uri: str) -> dict:
    bucket, _, key = manifest_uri[5:].partition("/")
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except Exception:
        return {}


def _record_stage_event(ddb, run_id: str, stage: str, event_name: str, detail: dict):
    table = ddb.Table(os.environ["EVENTS_TABLE"])
    table.put_item(Item={
        "run_id": run_id,
        "sk": f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}#{stage}#{event_name}",
        "detail": json.dumps(detail, default=str),
    })


#: A human verdict addressed to a run, parked where the run's own events live. The sk
#: prefix keeps directives in one contiguous query range and out of the timeline the
#: console renders.
DIRECTIVE_SK = "directive#"


#: A run in one of these states has no live driver invocation, so nothing will ever
#: call take_directive() for it again. Derived from the only two writers of a terminal
#: runs.status -- the driver's own handle_escalate ("escalated") and the state machine's
#: MarkRunFailed / MarkRunDone ("failed" / "completed") -- plus the manual-stop marker.
#: `running` is deliberately absent: that is the ONE state with a listener.
UNREACHABLE_RUN_STATES = ("escalated", "failed", "completed", "stopped")


def run_can_hear_a_directive(ddb, run_id: str) -> tuple[bool, str]:
    """Can a directive addressed to `run_id` still reach an agent? (reachable, status).

    take_directive has exactly one caller: the checkpoint branch of a LIVE driver
    invocation. So "delivered" is not a property of the write -- it is a property of
    whether anyone will ever read it. A verdict parked for a run whose execution has
    ended sits in a mailbox nobody will open again.

    Unknown or unreadable status returns reachable=True. A directives lookup that
    fails must not be the reason a verdict is withheld from a run that could act on
    it; the failure mode we are fixing is a SILENT no-op, and refusing to deliver on
    a transient DDB error would invent a second one.
    """
    try:
        row = ddb.Table(os.environ["RUNS_TABLE"]).get_item(
            Key={"run_id": run_id}).get("Item") or {}
    except Exception:  # noqa: BLE001 — see docstring: unknown means "try to deliver"
        return True, ""
    status = str(row.get("status", ""))
    if not row:
        return True, ""
    return not status.startswith(UNREACHABLE_RUN_STATES), status


def put_directive(ddb, run_id: str, decision: str, rationale: str = "",
                  adjusted_params: dict | None = None, actor: str = "conductor") -> dict:
    """Park a verdict where the agent working `run_id` will pick it up.

    Until this existed, a stage agent could ASK a blocking question (checkpoint /
    escalate_human) but nothing could ANSWER it: resolve_escalation wrote an
    EscalationResolved stage-event that no reader consumed, so triage was advice
    delivered into the void. Live, data-prep proved its own approved teacher budget
    infeasible -- 13.5k output tokens per attempt against a plan that assumed 1,800 --
    and then went on spending under that cap, because "continue" was the only answer
    the driver knew how to give.

    Returns {"parked": bool, "reachable": bool, "run_status": str}. The verdict is
    still WRITTEN when unreachable -- it is the audit record of what was decided --
    but `reachable: False` is what stops the caller reporting success. Answering an
    escalation whose run has already ended is not an answer, and a channel that
    returns 200 for it is how #16 stayed open for three days: the tool reported
    "resolved" every time, so nothing looked broken.
    """
    reachable, status = run_can_hear_a_directive(ddb, run_id)
    ddb.Table(os.environ["EVENTS_TABLE"]).put_item(Item={
        "run_id": run_id,
        "sk": f"{DIRECTIVE_SK}{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        "decision": decision,
        "rationale": str(rationale)[:2000],
        "adjusted_params": json.dumps(adjusted_params or {}, default=str),
        "actor": actor,
        "delivered": False,
        "deliverable": reachable,
        **({"run_status_at_put": status} if status else {}),
    })
    return {"parked": True, "reachable": reachable, "run_status": status}


def take_directive(ddb, run_id: str) -> Optional[dict]:
    """Pop the oldest undelivered directive for this run, or None.

    Delivered exactly once, by a conditional update: a verdict redelivered on every
    checkpoint reads as a fresh instruction each time, and an agent told "raise the cap
    to $13" on every breath would raise it repeatedly. The condition also makes two
    concurrent drivers safe -- the loser sees ConditionalCheckFailed and moves on.

    Never raises. A directives lookup that fails must degrade to "no directive", not
    stall a run that merely wanted another turn.
    """
    try:
        table = ddb.Table(os.environ["EVENTS_TABLE"])
        rows = table.query(
            KeyConditionExpression=Key("run_id").eq(run_id)
            & Key("sk").begins_with(DIRECTIVE_SK)).get("Items", [])
        for row in sorted(rows, key=lambda r: r.get("sk", "")):
            if not str(row.get("sk", "")).startswith(DIRECTIVE_SK) or row.get("delivered"):
                continue
            try:
                table.update_item(
                    Key={"run_id": run_id, "sk": row["sk"]},
                    UpdateExpression="SET delivered = :t, delivered_at = :now",
                    ConditionExpression="delivered = :f",
                    ExpressionAttributeValues={
                        ":t": True, ":f": False,
                        ":now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
            except Exception:  # noqa: BLE001 — another driver claimed it first
                continue
            params = row.get("adjusted_params") or "{}"
            return {"decision": row.get("decision", ""),
                    "rationale": row.get("rationale", ""),
                    "adjusted_params": (json.loads(params)
                                        if isinstance(params, str) else dict(params)),
                    "actor": row.get("actor", "conductor")}
    except Exception:  # noqa: BLE001 — no directive beats a stalled run
        return None
    return None


def _gate_event(metrics: dict) -> str:
    return ev.QUALITY_GATE_PASSED if metrics.get("gate_passed") else ev.QUALITY_GATE_FAILED


def _health_event(metrics: dict) -> Optional[str]:
    """DriftDetected on a monitor health task, or nothing.

    DRIFT_DETECTED has been declared in events.py since Phase 1 and emitted by NOTHING,
    while the monitor prompt tells the agent to put its finding in
    ``metrics.drift_detected`` "so the orchestrator can emit the event" — naming an emitter
    that does not exist. Nobody noticed because no monitor task had ever been dispatched;
    the promise and its absence were unobservable at the same time.

    It belongs here, not in the agent: emitting is the driver's job for every other stage
    (STAGE_EVENT_MAP), the harness role's PutEvents grant is scoped to this bus for exactly
    this reason, and an agent that self-reports an event can emit one for work it did not
    verify.

    Strict truthiness, deliberately matching the eval gate rather than ``bool()``: this
    event is the only signal a human gets that a deployed model has started behaving
    differently. ``is True`` means an absent or null finding stays silent instead of a
    string like "unknown" or "none" — which is truthy — announcing drift that nobody
    observed. The failure directions are asymmetric but both are real, so the rule is that
    the agent must say ``true`` to be heard.
    """
    return ev.DRIFT_DETECTED if metrics.get("drift_detected") is True else None


def _stamp_dispatch(norm: dict, stage: str, task: str) -> dict:
    """Overwrite the agent's echoed stage/task with what was actually DISPATCHED.

    ``normalize_stage_complete`` reads stage and task out of the agent's tool args, which
    is right for everything else in the payload -- outputs, metrics and evidence are the
    agent's findings and nobody else can supply them. Stage and task are the opposite kind
    of fact: the driver was told both in its own invocation event, so the agent's copy is
    at best a restatement and at worst wrong. Both live monitor sweeps recorded
    ``"task": ""`` because the agent simply omitted the field, and the run's row then said
    a monitor stage completed without saying WHICH of health/sweep/report did -- the exact
    ambiguity #58 existed to remove. It is not cosmetic: the console derives which
    (stage, task) pairs a run executed from this field (``_session_ids``), so an empty
    task quietly widens to "any task of that stage".

    The dispatch wins on principle, not just because the echo happened to be empty here:
    an agent that echoes ``task: "report"`` on a sweep invocation would otherwise file its
    sweep findings under a task that never ran.
    """
    return {**norm, "stage": stage or norm.get("stage", ""),
            "task": task or norm.get("task", "")}


def handle_stage_complete(c, event, args) -> dict:
    """Verify → normalize → canonical publish → events → settle token."""
    run_id, stage, task = event["run_id"], event["stage"], event["task"]
    norm = normalize_stage_complete(args)

    missing = verify_outputs(c["s3"], norm["outputs"])
    if missing:
        return {"ok": False, "missing_outputs": missing}

    _record_stage_event(c["ddb"], run_id, stage, "stage_complete",
                        _stamp_dispatch(norm, stage, task))

    if stage == "eval" and task == "gate":
        detail_type = _gate_event(norm.get("metrics", {}))
    elif stage == "monitor" and task == "health":
        detail_type = _health_event(norm.get("metrics", {}))
    else:
        detail_type = STAGE_EVENT_MAP.get((stage, task))
    if detail_type:
        ev.emit_event(os.environ["EVENT_BUS"], detail_type,
                      {"run_id": run_id, "stage": stage, **norm.get("metrics", {})},
                      client=c["events"])

    # Canonical report — the driver writes it; never rely on the agent's upload.
    #
    # ISOLATED from the token settle below, because it used to sit directly in front of
    # it and that cost a run. Live, data-prep finished teacher generation at its cap,
    # called stage_complete (twice, at 19:23:49 and 19:26:20), and this write died on
    # AccessDenied -- the driver had no s3:PutObject until 19:30. The exception left
    # send_task_success unreached, so the token parked for the full 7200s TimeoutSeconds
    # and the console showed "Data Prep · Generate failed" for work that was DONE and
    # verified on S3.
    #
    # The report is a dashboard convenience; the token is the pipeline's only way to
    # learn that a paid-for stage succeeded. Nothing about the former may be allowed to
    # withhold the latter -- so it degrades to a reported warning instead of a 2-hour
    # silence. The IAM gap is fixed too, but the ordering hazard is the general bug: any
    # future failure in this write (throttling, a bucket policy, a KMS denial) would
    # have bought the same outcome.
    report_error = None
    try:
        manifest = _load_manifest(c["s3"], event["manifest_uri"])
        if manifest:
            manifest.setdefault("stages", {})[stage] = {
                "status": "completed", "outputs": norm["outputs"],
                "metrics": norm.get("metrics", {}), "evidence": norm.get("evidence", "")}
            write_run_report(c["s3"], os.environ["DATA_BUCKET"], manifest)
    except Exception as exc:  # noqa: BLE001 — never withhold the token for a report
        report_error = f"{type(exc).__name__}: {exc}"
        print(f"[driver] canonical report FAILED for {run_id}/{stage}: {report_error}")
        _record_stage_event(c["ddb"], run_id, stage, "report_write_failed",
                            {"error": report_error})

    if event.get("task_token"):
        metrics = norm.get("metrics", {})
        payload = {"run_id": run_id, "stage": stage, "task": task, **metrics}
        # Gate semantics must be strict for gate tasks: an absent/None gate_passed on
        # an eval gate means NOT passed (fail closed) — a mini-run agent once emitted
        # gate_passed=null + needs_human=true and the old default-True promoted it.
        if stage == "eval" and task == "gate":
            payload["gate_passed"] = metrics.get("gate_passed") is True
        else:
            payload["gate_passed"] = bool(metrics.get("gate_passed", True))
        if report_error:
            payload["report_write_failed"] = report_error
        c["sfn"].send_task_success(taskToken=event["task_token"],
                                   output=json.dumps(payload, default=str))
    return {"ok": True, "normalized": norm,
            **({"report_error": report_error} if report_error else {})}


def handle_job_launched(c, event, args) -> dict:
    """Launch-and-release: park the token keyed by job name; resume λ settles it."""
    run_id = event["run_id"]
    table = c["ddb"].Table(os.environ["RUNS_TABLE"])
    table.update_item(
        Key={"run_id": run_id},
        UpdateExpression="SET job_name = :j, task_token = :t, current_stage = :s",
        ExpressionAttributeValues={":j": args.get("job_name", ""),
                                   ":t": event.get("task_token", ""),
                                   ":s": event["stage"]})
    ev.emit_event(os.environ["EVENT_BUS"], ev.TRAINING_STARTED,
                  {"run_id": run_id, "job_name": args.get("job_name", "")},
                  client=c["events"])
    return {"released": True}


#: The finops agent is the one harness that is NOT a pipeline stage: it is invoked by
#: the scheduler, not the state machine, so it has no task token, no run in the runs
#: table, and no manifest to append to. Its terminal tools are its own.
FINOPS_STAGE = "finops"
FINOPS_TERMINAL_TOOLS = ("publish_cost_report", "update_rate_card", "flag_variance")


def _is_finops(event) -> bool:
    return event.get("stage") == FINOPS_STAGE


def _mark_run_escalated(ddb, run_id: str) -> bool:
    """Set status=escalated on an EXISTING run row. True if a row was updated.

    `attribute_exists(run_id)` is the whole point. DynamoDB's update_item is an UPSERT:
    on a key with no row it CREATES one carrying the key plus whatever SET writes. So
    this call was never "update the run's status" -- for any invocation with no run, it
    was "mint a run", and the row it minted was the two-attribute shape
    {run_id, status: escalated} with no created_at, trigger_source or iteration.

    That is not hypothetical: `sweep-2026-08-01` sat in llmops-pipeline-runs from a
    scheduled orphan-endpoint sweep that escalated. monitor_sweep/handler.py is careful
    -- it writes its own bookkeeping row to EVENTS_TABLE and its docstring says why a
    sweep must never appear as a run -- and then the driver wrote one on its behalf,
    through a path the sweep does not know exists.

    The condition, rather than another `_is_finops`-style stage allowlist: the previous
    fix enumerated the one non-run invoker known at the time (finops), which left the
    NEXT one -- the sweep, added later, under its own synthetic sweep-<date> id -- to
    rediscover the same defect. Triage (triage-<subject>) and any future scheduled
    dispatch are the same shape. The runs table already knows which ids name runs; a
    list of stages that don't is a second copy of that fact, maintained by hand, that
    is wrong the moment someone adds a caller. Only start_pipeline creates run rows,
    which makes "a row exists" exactly the right question.

    A rejected condition is NOT an error here: it is the answer. Anything else raises,
    because a throttle or an outage must not read as "this was not a run".
    """
    try:
        ddb.Table(os.environ["RUNS_TABLE"]).update_item(
            Key={"run_id": run_id},
            UpdateExpression="SET #s = :v",
            ConditionExpression="attribute_exists(run_id)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":v": "escalated"})
        return True
    except Exception as exc:  # noqa: BLE001 — only the rejected condition is absorbed
        if _is_condition_failure(exc):
            return False
        raise


def _is_condition_failure(exc) -> bool:
    """ConditionalCheckFailedException, matched by botocore error CODE.

    Same reasoning as resume_pipeline's TASK_GONE_CODES: the exception classes hang off
    a live client instance, so referencing table.meta.client.exceptions here would make
    this module unimportable under an injected test double.
    """
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    return code == "ConditionalCheckFailedException"


def handle_escalate(c, event, args) -> dict:
    """Raise a stage's escalation to a human, on every channel that still works.

    The channels are deliberately independent, and the ordering matters. SNS used to be
    the first statement here and unwrapped, so a failed publish took the stage event, the
    bus event AND the task-token settle down with it -- the run would then sit at
    `running` holding a live token until the state machine's timeout, hours later, over a
    notification. That is the worst possible thing to gate on this particular call:
    `llmops-escalations` has **zero subscribers** live, so SNS is the one channel already
    known to reach nobody, and `ensure_topic` in deploy/03_storage.py reports it as
    "NO SUBSCRIBERS -- every escalate_human call publishes into the void" precisely
    because the deploy cannot invent an address. A channel that today delivers to no one
    must not be able to silence the two that do deliver: the stage event the console
    renders, and the EscalatedToHuman event the conductor triages off the bus.
    """
    run_id = event["run_id"]
    try:
        c["sns"].publish(TopicArn=os.environ["LLMOPS_SNS_TOPIC"],
                         Subject=f"[llmops] escalation: {run_id}/{event['stage']}",
                         Message=json.dumps(args, indent=2, default=str))
    except Exception as exc:  # noqa: BLE001 — one dead channel must not close the rest
        print(f"[driver] SNS publish failed for the escalation of {run_id}: {exc}")
    if _is_finops(event):
        # No runs-table row exists for an audit invocation; updating one would mint a
        # phantom run that the console would then display alongside real pipeline runs.
        # Kept as an early return even though _mark_run_escalated would now decline the
        # write anyway: the audit path also has no task token and no run to emit about,
        # so it exits before the event and the settle below, not just before the write.
        return {"escalated": True}
    run_row = _mark_run_escalated(c["ddb"], run_id)
    # Record the escalation in stage-events on BOTH paths. handle_escalate has never
    # written one -- for a real run the runs.status write was the whole durable record,
    # so an escalation has never appeared in the timeline the console renders from this
    # table, unlike a page (handle_page_human records its own). Declining the row write
    # for a non-run would have made that worse: a scheduled sweep's escalation would be
    # left with nothing on record anywhere but an SNS email. Unconditional, so the trail
    # does not depend on which path ran; `run_row` says which it was, and false is the
    # signal that this id names no pipeline run. Bookkeeping only -- it must never be
    # able to withhold the alert below.
    try:
        _record_stage_event(c["ddb"], run_id, event["stage"], "escalated",
                            {"reason": args.get("reason", ""),
                             "task": event.get("task", ""), "run_row": run_row})
    except Exception as exc:  # noqa: BLE001 — never withhold an escalation for a log
        print(f"[driver] could not record the escalation of {run_id}: {exc}")
    try:
        ev.emit_event(os.environ["EVENT_BUS"], ev.ESCALATED_TO_HUMAN,
                      {"run_id": run_id, "stage": event["stage"],
                       "reason": args.get("reason", "")}, client=c["events"])
    except Exception as exc:  # noqa: BLE001 — see below: the token must still settle
        # The settle is what releases the state machine. Letting a failed PutEvents skip
        # it would park a live task token on a run that has already escalated, and the
        # only thing that ever frees it is the stage's own timeout (7200s for data_prep,
        # 21600s for finetune) -- the zombie that MarkRunDone/MarkRunFailed and #52 all
        # exist to prevent, re-entered through the notification path.
        print(f"[driver] could not emit {ev.ESCALATED_TO_HUMAN} for {run_id}: {exc}")
    if event.get("task_token"):
        c["sfn"].send_task_failure(taskToken=event["task_token"],
                                   error="EscalatedToHuman",
                                   cause=args.get("reason", "")[:250])
    return {"escalated": True}


def handle_page_human(c, event, args: dict) -> dict:
    """Escalate a triage decision to the human owner: SNS brief + audit event.

    `page_human` is the conductor's ONLY exit when a decision is above its authority,
    and it is the exit the driver names in its own undeliverable-verdict rejection.
    It was declared on the orchestrator harness from Phase 5 on and serviced only by
    the console's chat worker -- so on the DRIVER path (every triage invocation: the
    conductor is not in a chat when an EscalatedToHuman event routes to it) it fell
    through to the unknown-tool branch and came back {"status": "unsupported"}.

    Live proof, 2026-08-01 13:45Z: the fix to resolve_escalation (#53) correctly told
    the conductor its verdict was undeliverable and to use launch_run or page_human.
    The conductor did neither -- it re-called resolve_escalation, was rejected again,
    then wrote plan.json + relaunch-plan.json to S3 and the turn ended. No run was
    dispatched and no human was paged. A rejection that names two paths where one of
    them silently answers "unsupported" is a dead end dressed as a choice.

    The paging itself is NOT trust-but-verify'd against anything, because there is
    nothing to verify: the brief IS the artifact. What is enforced is that a page
    carries a decision brief -- situation + recommendation, matching the harness's own
    required schema. A page reading "needs a human" with no options tells the owner
    only that something is wrong, which is the state they were already in.
    """
    situation = str(args.get("situation") or args.get("reason") or "").strip()
    recommendation = str(args.get("recommendation") or "").strip()
    if not situation or not recommendation:
        return {"ok": False, "reason": (
            "page_human needs both 'situation' and 'recommendation': a page without a "
            "recommendation hands the owner the problem and none of the analysis you "
            "already did. Include 'options' too if there is a real choice to make.")}

    subject_run = str(args.get("run_id") or event.get("run_id") or "")
    brief = {"run_id": subject_run,
             "situation": situation,
             "options": args.get("options") or [],
             "recommendation": recommendation,
             "paged_by": "orchestrator-triage",
             "triaging_run_id": event.get("run_id", "")}
    c["sns"].publish(
        TopicArn=os.environ["LLMOPS_SNS_TOPIC"],
        Subject=f"[llmops] owner decision needed: {subject_run or 'triage'}"[:100],
        Message=json.dumps(brief, indent=2, default=str))
    # Recorded against the run being escalated ABOUT, not the triaging run -- same
    # addressing rule as put_directive, for the same reason: the timeline a reader
    # opens is the stuck run's, not the conductor's.
    _record_stage_event(c["ddb"], subject_run or event.get("run_id", ""),
                        "orchestrator", "HumanPaged", brief)
    # OwnerPaged, NOT EscalatedToHuman. A page is what the conductor emits when it has
    # ALREADY triaged and found the decision above its authority; EscalatedToHuman means
    # "a conductor should look at this". Sharing one detail-type made the triage rule
    # feed itself the moment that rule existed -- escalate -> triage -> page -> triage --
    # and every lap is a real harness invocation billed against a decision already made.
    ev.emit_event(os.environ["EVENT_BUS"], ev.OWNER_PAGED,
                  {"run_id": subject_run, "stage": "orchestrator",
                   "reason": situation[:500]}, client=c["events"])
    return {"ok": True, "run_id": subject_run}


def handle_finops_tool(c, event, name: str, args: dict) -> dict:
    """Service one of the finops agent's terminal tools.

    Same trust-but-verify contract as ``stage_complete``: a claimed S3 artifact is
    head_object'd before the call is accepted, because an agent that reports a report
    it never wrote produces a dashboard panel pointing at a 404.

    Two rejections are specific to cost data and are worth stating in code rather than
    only in the prompt:

    - A report for a period Cost Explorer still marks ``Estimated`` must declare
      ``settlement: provisional``. A provisional number published as settled is how a
      figure that will still move gets quoted as final.
    - ``flag_variance`` must name a ``driver`` category. One aggregate percentage says
      the estimate was wrong without saying what to fix, and the whole point of
      reconciliation is that the next estimate is better.

    Rejections come back as a toolResult so the agent can correct and re-call, exactly
    as stage_complete's missing-output path does — the turn is not wasted.
    """
    period = event.get("params", {}).get("period", "")
    project = event.get("params", {}).get("project", "")

    if name == "publish_cost_report":
        missing = verify_outputs(c["s3"], [args.get("report_uri", "")])
        if missing:
            return {"ok": False, "reason": f"report_uri not in S3: {missing}. "
                                          "Write it and call publish_cost_report again."}
        if args.get("settlement") not in ("provisional", "settled"):
            return {"ok": False, "reason": "settlement must be 'provisional' or "
                                          "'settled'; a period Cost Explorer flagged "
                                          "Estimated is provisional."}
    elif name == "flag_variance":
        if not args.get("driver"):
            return {"ok": False, "reason": "flag_variance needs 'driver': the single "
                                          "category driving the delta. A percentage "
                                          "alone does not say what to fix."}
    elif name == "update_rate_card":
        missing = verify_outputs(c["s3"], [args.get("rates_uri", "")])
        if missing:
            return {"ok": False, "reason": f"rates_uri not in S3: {missing}."}

    # Audit trail keyed (project, period) so a missing daily reconcile is visible, and
    # so the console can read a period's findings without scanning.
    c["ddb"].Table(os.environ["ACTUALS_TABLE"]).put_item(Item={
        "project": project or "unknown",
        "sk": f"{period}#finding#{name}#{args.get('run_id', '-')}",
        "task": event.get("task", ""),
        "tool": name,
        "detail": json.dumps(args, default=str)[:8000],
    })

    # A variance the agent flagged is a human-facing finding, not just a log line.
    if name == "flag_variance":
        try:
            c["sns"].publish(
                TopicArn=os.environ["LLMOPS_SNS_TOPIC"],
                Subject=f"[llmops] cost variance: {args.get('run_id', '?')} "
                        f"{args.get('variance_pct', '?')}%",
                Message=json.dumps(args, indent=2, default=str))
        except Exception as exc:  # noqa: BLE001 — a notify failure must not lose the row
            print(f"[finops] variance notify failed: {exc}")

    return {"ok": True, "tool": name, "args": args}


#: Fallback chain for vendor-quota 5xx bursts (AGENTS.md: failover is a design layer).
MODEL_FALLBACKS = {
    "global.anthropic.claude-fable-5": "global.anthropic.claude-opus-5",
    "us.anthropic.claude-fable-5": "global.anthropic.claude-opus-5",
}


def _is_model_5xx(error: str) -> bool:
    return any(sig in error for sig in
               ("InternalServerException", "ServiceUnavailableException"))


def _maybe_failover_model(c, event) -> None:
    """Hot-swap the harness to its fallback model on a vendor 5xx burst.
    Best-effort: any failure here must not break the salvage retry."""
    try:
        region = os.environ.get("AWS_REGION", "us-east-1")
        ctl = boto3.client("bedrock-agentcore-control", region_name=region)
        arn = _resolve_harness_arn(event["harness_id"])
        harness_full_id = arn.rsplit("/", 1)[-1]
        h = ctl.get_harness(harnessId=harness_full_id)["harness"]
        model = h["model"]
        current = model.get("bedrockModelConfig", {}).get("modelId", "")
        fallback = MODEL_FALLBACKS.get(current)
        if not fallback:
            return
        model["bedrockModelConfig"]["modelId"] = fallback
        ctl.update_harness(harnessId=harness_full_id, model=model,
                           clientToken=hashlib.sha256(
                               f"{harness_full_id}-{fallback}".encode()).hexdigest()[:40])
        for _ in range(24):  # wait READY (~15s typical)
            if ctl.get_harness(harnessId=harness_full_id)["harness"]["status"] == "READY":
                break
            time.sleep(5)
        # ModelFailedOver, NOT EscalatedToHuman. Nothing is being escalated: the
        # failover already fixed it and the retry continues. This carried the word
        # "informational" inside a reason string, which was harmless only while the bus
        # had no rules -- an EventBridge pattern cannot read prose, so the first rule to
        # route EscalatedToHuman to triage would have paged the conductor about a run
        # that had just healed itself.
        ev.emit_event(os.environ["EVENT_BUS"], ev.MODEL_FAILED_OVER, {
            "run_id": event.get("run_id", "?"), "stage": event.get("stage", "?"),
            "from_model": current, "to_model": fallback,
            "reason": f"ModelFailover: {current} -> {fallback} (vendor 5xx burst); "
                      "informational, pipeline continuing"}, client=c["events"])
    except Exception:  # noqa: BLE001 — never let failover break the retry path
        pass


RE_ASK = ("Your turn ended without an inline-function call. If your task is "
          "INCOMPLETE, continue now and finish it — then call the appropriate "
          "inline function (job_launched for launched jobs, stage_complete for "
          "completed stages, checkpoint if you need another turn). If the work "
          "is already done, call stage_complete now (outputs may be an empty "
          "list if nothing was produced).")


def handler(event, context=None, clients=None):
    """Run one stage, and never strand the task token on the way out.

    A SYNCHRONOUS stage invocation that raises is reported to Step Functions by the
    Lambda integration itself. An ASYNCHRONOUS continuation is not: the state machine
    waits on the task token, not on this invocation, so an exception here goes to
    CloudWatch and to nobody else. The token then parks until TimeoutSeconds -- 7200s
    for data-prep, 21600s for finetune -- with the run record still saying 'running'.

    Live: the driver's missing s3:PutObject grant crashed the final stage_complete of
    run-...-8b864805 twice in the same silence, and the run held its token for 90
    minutes. The AccessDenied was one bug. The 90 minutes was this one: the stage had
    genuinely failed and the only participant who knew was a log stream.

    So an unexpected exception fails the token with the real cause attached, then
    re-raises so the invocation is still recorded as an error (and a synchronous
    caller, like the console's dispatch path, still sees a hard failure rather than a
    silent success).
    """
    c = clients or _clients()
    # An EventBridge delivery is not a state-machine payload. Recognised by its own
    # envelope keys ("detail-type" + "detail") rather than by the absence of a task
    # token, because plenty of legitimate driver invocations have no token.
    if event.get("detail-type") == ev.ESCALATED_TO_HUMAN and "detail" in event:
        event = triage_event_from_bus(event, os.environ["DATA_BUCKET"])
    try:
        return _run_stage(event, context, c)
    except Exception as exc:
        token = event.get("task_token")
        if token:
            try:
                c["sfn"].send_task_failure(
                    taskToken=token, error="DriverCrashed",
                    cause=f"{type(exc).__name__}: {exc}"[:32000])
            except Exception as report_exc:  # noqa: BLE001
                # Nothing left to do but say so; the timeout is now the only backstop.
                print(f"[driver] could not fail the parked token: {report_exc}")
        raise


def _run_stage(event, context=None, c=None):
    c = c if c is not None else _clients()
    sess = session_id(event["run_id"], event["stage"], event["task"])
    payload = {"run_id": event["run_id"], "stage": event["stage"],
               "manifest_uri": event["manifest_uri"],
               "params": {"task": event["task"], **(event.get("params") or {})}}
    if event.get("task_token"):
        payload["params"]["iteration"] = event.get("iteration", 0)

    # Continuation across Lambda invocations: one harness turn can run 840s and the
    # Lambda dies at 900s, so only ONE turn fits per invocation. Whenever the loop
    # would start another turn without enough time left, self-reinvoke carrying the
    # pending messages list (session + task token survive; live-verified Sandbox.Timedout
    # killed a run whose agent finished its work but never got to report it).
    if event.get("_continuation"):
        messages = event["_continuation"]
        stream_retried = bool(event.get("_stream_retried"))
        re_asks = int(event.get("_re_asks", 0))
    else:
        messages = _user_text(json.dumps(payload, default=str))
        stream_retried = False
        re_asks = 0  # up to 2: continue-and-finish nudge, then final demand

    def _out_of_time() -> bool:
        return bool(context) and context.get_remaining_time_in_millis() < 850_000

    def _self_reinvoke():
        c["lambda"].invoke(
            FunctionName=context.function_name, InvocationType="Event",
            Payload=json.dumps({**event, "_continuation": messages,
                                "_stream_retried": stream_retried,
                                "_re_asks": re_asks}, default=str))
        return {"status": "self_reinvoked_between_turns"}

    first_turn = True

    while True:
        if not first_turn and _out_of_time():
            return _self_reinvoke()
        first_turn = False
        resp = _invoke(c["agentcore"], event["harness_id"], sess, messages,
                       event.get("qualifier"))
        out = _drain(resp)

        if out["error"] and not stream_retried:
            # involuntary stream death — same-session salvage retry, once
            stream_retried = True
            if _is_model_5xx(out["error"]):
                _maybe_failover_model(c, event)  # vendor-quota burst: hot-swap model
            messages = _user_text("The stream was interrupted. Continue from where "
                                  "you left off; call your pending inline function.")
            continue

        tu = out["tool_use"]
        # Only a stopReason of "tool_use" means the harness is WAITING for a result.
        # A toolUse block riding along with end_turn was already serviced inside the
        # harness; replying to it makes the next ConverseStream invalid with
        # "toolResult blocks ... exceeds the number of toolUse blocks of previous
        # turn" (found live on the console's dispatch path, same shape here).
        if tu and out["stop_reason"] == "tool_use":
            name, args = tu["name"], tu.get("input") or {}
            if name == "stage_complete":
                result = handle_stage_complete(c, event, args)
                if not result["ok"]:
                    messages = _tool_result_content(tu, {
                        "status": "rejected",
                        "reason": f"claimed outputs missing from S3: {result['missing_outputs']}. "
                                  "Write them and call stage_complete again."})
                    continue
                _invoke(c["agentcore"], event["harness_id"], sess,
                        _tool_result_content(tu, {"status": "acknowledged"}),
                        event.get("qualifier"))
                return {"status": "completed", **result["normalized"]}
            if name == "job_launched":
                handle_job_launched(c, event, args)
                _invoke(c["agentcore"], event["harness_id"], sess,
                        _tool_result_content(tu, {"status": "released"}),
                        event.get("qualifier"))
                return {"status": "released", "job_name": args.get("job_name")}
            if name == "checkpoint":
                # Just answer it and let the loop-top _out_of_time() check own the
                # reinvoke. This branch used to reinvoke itself with {"_resumed": True}
                # -- a key nothing reads -- so the resumed invocation fell through to
                # the fresh-start branch, re-sent the original stage prompt, and
                # silently dropped both the pending toolResult and the work already
                # paid for (live: a budget escalation raised after a pilot found the
                # plan's token estimate 6.5x low). Its own <60s guard was also dead
                # code, since _out_of_time() fires at 850s remaining, far earlier.
                #
                # A checkpoint is also the DELIVERY point for a human verdict. It is
                # the one moment a working agent is guaranteed to be listening, so a
                # directive parked by resolve_escalation (or by an operator) rides back
                # in the toolResult here. Without this the answer channel was
                # write-only: the agent could ask and nothing could reply.
                directive = take_directive(c["ddb"], event["run_id"])
                messages = _tool_result_content(tu, {
                    "status": "directive", "directive": directive} if directive
                    else {"status": "continue"})
                continue
            if name == "escalate_human":
                handle_escalate(c, event, args)
                _invoke(c["agentcore"], event["harness_id"], sess,
                        _tool_result_content(tu, {"status": "escalated"}),
                        event.get("qualifier"))
                return {"status": "escalated"}
            if name == "resolve_escalation":
                # Conductor triage verdict: record it where the escalation lives
                # (stage-events) and emit the resolution event. Discovered unserviced
                # by the same drift guard that found launch_run — a triage that ends
                # in "unsupported" leaves the pipeline paused forever.
                #
                # The audit record alone was still a verdict nobody heard: the waiting
                # agent reads directives, not the timeline. So the decision is ALSO
                # parked for delivery — addressed to the run being triaged, never to
                # the conductor's own run, or it lands in the triager's mailbox and the
                # stuck run waits forever.
                #
                # And parking it is still not answering it. take_directive has exactly
                # ONE caller -- the checkpoint branch of a LIVE driver invocation -- so
                # a verdict addressed to a run whose execution has ENDED goes into a
                # mailbox nobody will open again. This branch used to return
                # {"status": "resolved"} either way, which is how #16 stayed open for
                # three days: run-20260729T104648Z-41631739 was already `escalated`
                # with its token failed and its execution FAILED at 11:19:55Z, so
                # triaging it would have reported success and changed nothing. Same
                # class as the stranded task token: the write is authorized, and
                # unreachable. A tool that cannot act must say so, not return 200.
                subject = args.get("run_id") or ""
                _record_stage_event(c["ddb"], subject or event.get("run_id", ""),
                                    "orchestrator", "EscalationResolved",
                                    {"decision": args.get("decision"),
                                     "rationale": str(args.get("rationale", ""))[:500],
                                     "adjusted_params": args.get("adjusted_params") or {}})
                if subject:
                    parked = put_directive(
                        c["ddb"], subject,
                        decision=str(args.get("decision", "")),
                        rationale=str(args.get("rationale", "")),
                        adjusted_params=args.get("adjusted_params") or {},
                        actor="conductor")
                    if not parked["reachable"]:
                        # Rejected back to the conductor, not returned as a verdict:
                        # a rejection it can still act on (relaunch the stage via
                        # launch_run, or page_human) in the SAME turn. Returning here
                        # would end the triage having done nothing, which is the bug.
                        messages = _tool_result_content(tu, {
                            "status": "undeliverable",
                            "run_status": parked["run_status"],
                            "reason": (
                                f"run {subject} is {parked['run_status']}: its execution "
                                "has ended, so no agent will ever read this directive. "
                                "The decision is recorded for audit but CHANGES NOTHING. "
                                "To act on it, relaunch the work with launch_run "
                                "(carrying your adjusted_params), or call page_human if "
                                "that is above your authority.")})
                        continue
                _invoke(c["agentcore"], event["harness_id"], sess,
                        _tool_result_content(tu, {"status": "recorded"}),
                        event.get("qualifier"))
                return {"status": "resolved", "decision": args.get("decision"),
                        "run_id": args.get("run_id")}
            if name == "page_human":
                # The conductor's above-authority exit, and one of the two paths the
                # undeliverable-verdict rejection above tells it to take. Unserviced on
                # this path until now: declared on the harness since Phase 5, handled
                # only by the console chat worker, so every triage page answered
                # "unsupported" and the owner was never told. Terminal for the turn --
                # a page is a handoff, so continuing to prompt the agent would have it
                # keep deciding after it just said it could not.
                result = handle_page_human(c, event, args)
                if not result["ok"]:
                    messages = _tool_result_content(tu, {
                        "status": "rejected", "reason": result["reason"]})
                    continue
                _invoke(c["agentcore"], event["harness_id"], sess,
                        _tool_result_content(tu, {"status": "paged"}),
                        event.get("qualifier"))
                return {"status": "paged", "run_id": result["run_id"]}
            if name == "write_report":
                # trust-but-verify, same as every artifact claim
                missing = verify_outputs(c["s3"], [args.get("report_uri", "")])
                if missing:
                    messages = _tool_result_content(tu, {
                        "status": "rejected",
                        "reason": f"report_uri not in S3: {missing}. Write it first."})
                    continue
                _invoke(c["agentcore"], event["harness_id"], sess,
                        _tool_result_content(tu, {"status": "recorded"}),
                        event.get("qualifier"))
                return {"status": "completed", "report_uri": args.get("report_uri"),
                        "headline": str(args.get("headline", ""))[:300]}
            if name == "launch_run":
                # The conductor's dispatch tool, serviced through the same shared
                # module the console's Tasks tab uses — one implementation, no drift.
                # From Phase 5 until this branch existed, launch_run fell through to
                # the unknown-tool fallthrough below and every conductor plan died
                # with {"status": "unsupported"}.
                result = conductor_tools.service_launch_run(
                    c["lambda"], c["s3"], _kms(c), args,
                    os.environ.get("START_FN", "llmops-start-pipeline"),
                    expected=(event.get("params") or {}).get("approval_context"))
                if not result["ok"]:
                    messages = _tool_result_content(tu, {
                        "status": "rejected", "reason": result["reason"]})
                    continue
                _invoke(c["agentcore"], event["harness_id"], sess,
                        _tool_result_content(tu, {
                            "status": "dispatched", "run_id": result["run_id"]}),
                        event.get("qualifier"))
                return {"status": "dispatched", "run_id": result["run_id"],
                        "manifest_uri": result["manifest_uri"],
                        "execution_arn": result["execution_arn"]}
            if name in FINOPS_TERMINAL_TOOLS and _is_finops(event):
                result = handle_finops_tool(c, event, name, args)
                if not result["ok"]:
                    messages = _tool_result_content(tu, {
                        "status": "rejected", "reason": result["reason"]})
                    continue
                # flag_variance is a FINDING, not the end of the task: an audit can
                # flag several runs in one turn, so acknowledge and let it continue.
                # The report/rate-card calls are terminal.
                ack = _tool_result_content(tu, {"status": "recorded"})
                if name == "flag_variance":
                    messages = ack
                    continue
                _invoke(c["agentcore"], event["harness_id"], sess, ack,
                        event.get("qualifier"))
                return {"status": "completed", "tool": name, **result["args"]}
            # unknown tool — acknowledge and continue rather than dying
            messages = _tool_result_content(tu, {"status": "unsupported"})
            continue

        # Stream ended without a tool call.
        if re_asks < 2:
            re_asks += 1
            messages = _user_text(RE_ASK)
            continue

        # Re-asks exhausted → treat as stage failure.
        if event.get("task_token"):
            c["sfn"].send_task_failure(taskToken=event["task_token"],
                                       error="MissingStageComplete",
                                       cause=out["text"][:250])
        ev.emit_event(os.environ["EVENT_BUS"], ev.PIPELINE_FAILED,
                      {"run_id": event["run_id"], "stage": event["stage"],
                       "reason": "missing stage_complete"}, client=c["events"])
        return {"status": "failed", "reason": "missing stage_complete",
                "text_tail": out["text"][-500:]}
