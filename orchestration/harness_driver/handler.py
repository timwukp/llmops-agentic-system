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
    ("deploy", "deploy"): ev.MODEL_DEPLOYED,
    ("deploy", "smoke"): ev.SMOKE_TEST_PASSED,
    ("deploy", "teardown"): ev.ENDPOINT_DELETED,
}

_AGENTCORE_CFG = BotoConfig(read_timeout=870, connect_timeout=30,
                            retries={"max_attempts": 0})


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


def put_directive(ddb, run_id: str, decision: str, rationale: str = "",
                  adjusted_params: dict | None = None, actor: str = "conductor"):
    """Park a verdict where the agent working `run_id` will pick it up.

    Until this existed, a stage agent could ASK a blocking question (checkpoint /
    escalate_human) but nothing could ANSWER it: resolve_escalation wrote an
    EscalationResolved stage-event that no reader consumed, so triage was advice
    delivered into the void. Live, data-prep proved its own approved teacher budget
    infeasible -- 13.5k output tokens per attempt against a plan that assumed 1,800 --
    and then went on spending under that cap, because "continue" was the only answer
    the driver knew how to give.
    """
    ddb.Table(os.environ["EVENTS_TABLE"]).put_item(Item={
        "run_id": run_id,
        "sk": f"{DIRECTIVE_SK}{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        "decision": decision,
        "rationale": str(rationale)[:2000],
        "adjusted_params": json.dumps(adjusted_params or {}, default=str),
        "actor": actor,
        "delivered": False,
    })


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


def handle_stage_complete(c, event, args) -> dict:
    """Verify → normalize → canonical publish → events → settle token."""
    run_id, stage, task = event["run_id"], event["stage"], event["task"]
    norm = normalize_stage_complete(args)

    missing = verify_outputs(c["s3"], norm["outputs"])
    if missing:
        return {"ok": False, "missing_outputs": missing}

    _record_stage_event(c["ddb"], run_id, stage, "stage_complete", norm)

    if stage == "eval" and task == "gate":
        detail_type = _gate_event(norm.get("metrics", {}))
    else:
        detail_type = STAGE_EVENT_MAP.get((stage, task))
    if detail_type:
        ev.emit_event(os.environ["EVENT_BUS"], detail_type,
                      {"run_id": run_id, "stage": stage, **norm.get("metrics", {})},
                      client=c["events"])

    # Canonical report — the driver writes it; never rely on the agent's upload.
    manifest = _load_manifest(c["s3"], event["manifest_uri"])
    if manifest:
        manifest.setdefault("stages", {})[stage] = {
            "status": "completed", "outputs": norm["outputs"],
            "metrics": norm.get("metrics", {}), "evidence": norm.get("evidence", "")}
        write_run_report(c["s3"], os.environ["DATA_BUCKET"], manifest)

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
        c["sfn"].send_task_success(taskToken=event["task_token"],
                                   output=json.dumps(payload, default=str))
    return {"ok": True, "normalized": norm}


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


def handle_escalate(c, event, args) -> dict:
    run_id = event["run_id"]
    c["sns"].publish(TopicArn=os.environ["LLMOPS_SNS_TOPIC"],
                     Subject=f"[llmops] escalation: {run_id}/{event['stage']}",
                     Message=json.dumps(args, indent=2, default=str))
    if _is_finops(event):
        # No runs-table row exists for an audit invocation; updating one would mint a
        # phantom run that the console would then display alongside real pipeline runs.
        return {"escalated": True}
    c["ddb"].Table(os.environ["RUNS_TABLE"]).update_item(
        Key={"run_id": run_id},
        UpdateExpression="SET #s = :v",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":v": "escalated"})
    ev.emit_event(os.environ["EVENT_BUS"], ev.ESCALATED_TO_HUMAN,
                  {"run_id": run_id, "stage": event["stage"],
                   "reason": args.get("reason", "")}, client=c["events"])
    if event.get("task_token"):
        c["sfn"].send_task_failure(taskToken=event["task_token"],
                                   error="EscalatedToHuman",
                                   cause=args.get("reason", "")[:250])
    return {"escalated": True}


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
        ev.emit_event(os.environ["EVENT_BUS"], ev.ESCALATED_TO_HUMAN, {
            "run_id": event.get("run_id", "?"), "stage": event.get("stage", "?"),
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
    c = clients or _clients()
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
                subject = args.get("run_id") or ""
                _record_stage_event(c["ddb"], subject or event.get("run_id", ""),
                                    "orchestrator", "EscalationResolved",
                                    {"decision": args.get("decision"),
                                     "rationale": str(args.get("rationale", ""))[:500],
                                     "adjusted_params": args.get("adjusted_params") or {}})
                if subject:
                    put_directive(c["ddb"], subject,
                                  decision=str(args.get("decision", "")),
                                  rationale=str(args.get("rationale", "")),
                                  adjusted_params=args.get("adjusted_params") or {},
                                  actor="conductor")
                _invoke(c["agentcore"], event["harness_id"], sess,
                        _tool_result_content(tu, {"status": "recorded"}),
                        event.get("qualifier"))
                return {"status": "resolved", "decision": args.get("decision"),
                        "run_id": args.get("run_id")}
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
