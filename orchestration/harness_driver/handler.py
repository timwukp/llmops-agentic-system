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
from typing import Any, Optional

import boto3
from botocore.config import Config as BotoConfig

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))  # repo layout
try:
    from pipeline.contracts import events as ev
    from pipeline.contracts.report import normalize_stage_complete, write_run_report
except ImportError:  # Lambda bundle layout (contracts vendored alongside)
    import events as ev  # type: ignore
    from report import normalize_stage_complete, write_run_report  # type: ignore

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


def _invoke(ac, harness_id: str, sess: str, content: list, qualifier: Optional[str]):
    kwargs = dict(harnessId=harness_id, runtimeSessionId=sess,
                  actorId="llmops-pipeline",
                  messages=[{"role": "user", "content": content}])
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


def _tool_result_content(tool_use_id: str, payload: dict) -> list:
    return [{"toolResult": {"toolUseId": tool_use_id,
                            "content": [{"json": payload}],
                            "status": "success"}}]


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
        payload = {"run_id": run_id, "stage": stage, "task": task, **norm.get("metrics", {})}
        payload["gate_passed"] = bool(norm.get("metrics", {}).get("gate_passed", True))
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


def handle_escalate(c, event, args) -> dict:
    run_id = event["run_id"]
    c["sns"].publish(TopicArn=os.environ["LLMOPS_SNS_TOPIC"],
                     Subject=f"[llmops] escalation: {run_id}/{event['stage']}",
                     Message=json.dumps(args, indent=2, default=str))
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


RE_ASK = ("You finished without calling stage_complete. Call stage_complete now "
          "with your results (outputs may be an empty list if nothing was produced).")


def handler(event, context=None, clients=None):
    c = clients or _clients()
    sess = session_id(event["run_id"], event["stage"], event["task"])
    payload = {"run_id": event["run_id"], "stage": event["stage"],
               "manifest_uri": event["manifest_uri"],
               "params": {"task": event["task"], **(event.get("params") or {})}}
    if event.get("task_token"):
        payload["params"]["iteration"] = event.get("iteration", 0)

    content = [{"text": json.dumps(payload, default=str)}]
    stream_retried = False
    re_asked = False

    while True:
        resp = _invoke(c["agentcore"], event["harness_id"], sess, content,
                       event.get("qualifier"))
        out = _drain(resp)

        if out["error"] and not stream_retried:
            # involuntary stream death — same-session salvage retry, once
            stream_retried = True
            content = [{"text": "The stream was interrupted. Continue from where "
                                "you left off; call your pending inline function."}]
            continue

        tu = out["tool_use"]
        if tu and out["stop_reason"] == "tool_use":
            name, args = tu["name"], tu.get("input") or {}
            if name == "stage_complete":
                result = handle_stage_complete(c, event, args)
                if not result["ok"]:
                    content = _tool_result_content(tu["toolUseId"], {
                        "status": "rejected",
                        "reason": f"claimed outputs missing from S3: {result['missing_outputs']}. "
                                  "Write them and call stage_complete again."})
                    continue
                _invoke(c["agentcore"], event["harness_id"], sess,
                        _tool_result_content(tu["toolUseId"], {"status": "acknowledged"}),
                        event.get("qualifier"))
                return {"status": "completed", **result["normalized"]}
            if name == "job_launched":
                handle_job_launched(c, event, args)
                _invoke(c["agentcore"], event["harness_id"], sess,
                        _tool_result_content(tu["toolUseId"], {"status": "released"}),
                        event.get("qualifier"))
                return {"status": "released", "job_name": args.get("job_name")}
            if name == "checkpoint":
                if context and context.get_remaining_time_in_millis() < 60_000:
                    c["lambda"].invoke(FunctionName=context.function_name,
                                       InvocationType="Event",
                                       Payload=json.dumps({**event, "_resumed": True}))
                    return {"status": "self_reinvoked"}
                content = _tool_result_content(tu["toolUseId"], {"status": "continue"})
                continue
            if name == "escalate_human":
                handle_escalate(c, event, args)
                _invoke(c["agentcore"], event["harness_id"], sess,
                        _tool_result_content(tu["toolUseId"], {"status": "escalated"}),
                        event.get("qualifier"))
                return {"status": "escalated"}
            # unknown tool — acknowledge and continue rather than dying
            content = _tool_result_content(tu["toolUseId"], {"status": "unsupported"})
            continue

        # Stream ended without a tool call.
        if not re_asked:
            re_asked = True
            content = [{"text": RE_ASK}]
            continue

        # Re-ask failed too → treat as stage failure.
        if event.get("task_token"):
            c["sfn"].send_task_failure(taskToken=event["task_token"],
                                       error="MissingStageComplete",
                                       cause=out["text"][:250])
        ev.emit_event(os.environ["EVENT_BUS"], ev.PIPELINE_FAILED,
                      {"run_id": event["run_id"], "stage": event["stage"],
                       "reason": "missing stage_complete"}, client=c["events"])
        return {"status": "failed", "reason": "missing stage_complete",
                "text_tail": out["text"][-500:]}
