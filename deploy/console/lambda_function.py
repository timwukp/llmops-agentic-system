#!/usr/bin/env python3
"""
LLMOps Admin — AWS Lambda handler (HTTP API Gateway).

Operator dashboard for the llmops-agentic-system pipeline: one Lambda serves the
dashboard HTML (GET /) and the JSON API (GET/POST /api/*). Design and most of the
code are ported from bedrock-agentcore-agent-ops-console
(github.com/timwukp/bedrock-agentcore-agent-ops-console).

Auth model (ported): GET routes are public read-only; every POST except /api/login
requires a Cognito access token (validated server-side via cognito-idp GetUser).

The frontend ships as frontend.html in the same zip and is read ONCE at cold start
into a module global — no giant inline HTML string in this file.

Env: CONSOLE_TABLE, RUNS_TABLE, EVENTS_TABLE, DATA_BUCKET (optional — falls back to
     SSM /llmops/storage/bucket), STATE_MACHINE (name, default llmops-pipeline),
     START_FN (default llmops-start-pipeline), COGNITO_POOL_ID, COGNITO_CLIENT_ID,
     JUDGE_MODEL, SPANS_SINCE, OPTIMIZE_HARNESS (default llmops_orchestrator).
"""
import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Key

REGION = os.environ.get("AWS_REGION", "us-east-1")
ACCOUNT_ID = os.environ.get("ACCOUNT_ID") or boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]

CONSOLE_TABLE = os.environ.get("CONSOLE_TABLE", "LlmopsAdminRuns")   # optimization drafts (opt- prefix)
RUNS_TABLE = os.environ.get("RUNS_TABLE", "llmops-pipeline-runs")    # pipeline runs (PK run_id)
EVENTS_TABLE = os.environ.get("EVENTS_TABLE", "llmops-stage-events") # stage events (PK run_id, SK sk)
STATE_MACHINE = os.environ.get("STATE_MACHINE", "llmops-pipeline")
SM_ARN = f"arn:aws:states:{REGION}:{ACCOUNT_ID}:stateMachine:{STATE_MACHINE}"
START_FN = os.environ.get("START_FN", "llmops-start-pipeline")
SELF_FUNCTION = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")
COGNITO_POOL_ID = os.environ.get("COGNITO_POOL_ID", "")
COGNITO_CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "")
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "global.anthropic.claude-opus-5")
SPANS_SINCE = os.environ.get("SPANS_SINCE", "2026-07-28T12:00:00Z")  # OTEL_TRACES_SAMPLER=always_on since
OPTIMIZE_HARNESS = os.environ.get("OPTIMIZE_HARNESS", "llmops_orchestrator")
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "")  # same-origin by default — leave empty

ctl = boto3.client("bedrock-agentcore-control", region_name=REGION)
data = boto3.client("bedrock-agentcore", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
lam = boto3.client("lambda", region_name=REGION)
sfn = boto3.client("stepfunctions", region_name=REGION)
sm = boto3.client("sagemaker", region_name=REGION)
ssm = boto3.client("ssm", region_name=REGION)
cw = boto3.client("cloudwatch", region_name=REGION)
logsc = boto3.client("logs", region_name=REGION)
brt = boto3.client("bedrock-runtime", region_name=REGION)
cognito = boto3.client("cognito-idp", region_name=REGION)
_ddb = boto3.resource("dynamodb", region_name=REGION)
console_tbl = _ddb.Table(CONSOLE_TABLE) if CONSOLE_TABLE else None
runs_tbl = _ddb.Table(RUNS_TABLE) if RUNS_TABLE else None
events_tbl = _ddb.Table(EVENTS_TABLE) if EVENTS_TABLE else None

EVAL_RESULTS_LG_PREFIX = "/aws/bedrock-agentcore/evaluations/results/"

# The 6 pipeline harnesses. Runtime name = "harness_" + harnessName.
HARNESS_NAMES = ["llmops_data_prep", "llmops_finetune", "llmops_eval",
                 "llmops_deploy", "llmops_monitor", "llmops_orchestrator"]
WATCHED_RUNTIMES = [f"harness_{n}" for n in HARNESS_NAMES]

# ── frontend: read ONCE at cold start (replaces the source repo's inline string) ─
_HERE = os.path.dirname(os.path.abspath(__file__))
try:
    with open(os.path.join(_HERE, "frontend.html"), encoding="utf-8") as _f:
        FRONTEND_HTML = _f.read()
except Exception as _e:  # zip built without frontend.html — fail visibly, not blank
    FRONTEND_HTML = f"<h1>frontend.html missing from bundle: {_e}</h1>"

_BUCKET_CACHE = None


def data_bucket():
    """S3 data bucket: env DATA_BUCKET, else SSM /llmops/storage/bucket (cached)."""
    global _BUCKET_CACHE
    if _BUCKET_CACHE:
        return _BUCKET_CACHE
    b = os.environ.get("DATA_BUCKET", "")
    if not b:
        b = ssm.get_parameter(Name="/llmops/storage/bucket")["Parameter"]["Value"]
    _BUCKET_CACHE = b
    return b


def _resolve_harness_id(name_or_id):
    """Accept a harnessId, a harnessName, or a short name; resolve via SSM first
    (/llmops/harness/<short-name>), then by listing."""
    if not name_or_id:
        name_or_id = OPTIMIZE_HARNESS
    short = name_or_id.replace("llmops_", "").replace("_", "-")
    try:
        return ssm.get_parameter(Name=f"/llmops/harness/{short}")["Parameter"]["Value"]
    except Exception:
        pass
    for h in ctl.list_harnesses().get("harnesses", []):
        hid = h.get("harnessId", "")
        if name_or_id in (hid, h.get("harnessName")) or hid.rsplit("-", 1)[0] == name_or_id:
            return hid
    return name_or_id  # last resort: assume it already is an id


def session_id(run_id, stage, task):
    """Deterministic, >=33 chars — ported from orchestration/harness_driver so the
    console reconstructs the exact session ids the pipeline used."""
    base = f"{run_id}-{stage}-{task}"
    if len(base) >= 33:
        return base[:100]
    return (base + "-" + hashlib.sha256(base.encode()).hexdigest())[:64]


# ── overview: fleet + runs + executions ───────────────────────────────────────
def list_fleet():
    out = []
    for h in ctl.list_harnesses().get("harnesses", []):
        if not str(h.get("harnessName", "")).startswith("llmops_"):
            continue
        item = {"name": h["harnessName"], "id": h["harnessId"], "status": h["status"],
                "version": h.get("harnessVersion")}
        try:
            d = ctl.get_harness(harnessId=h["harnessId"])["harness"]
            item["model"] = d.get("model", {}).get("bedrockModelConfig", {}).get("modelId")
            item["skillsCount"] = len(d.get("skills", []) or [])
            item["maxIterations"] = d.get("maxIterations")
            item["timeoutSeconds"] = d.get("timeoutSeconds")
            item["status"] = d.get("status", item["status"])
        except Exception as e:
            item["detailError"] = str(e)[:120]
        out.append(item)
    out.sort(key=lambda x: HARNESS_NAMES.index(x["name"]) if x["name"] in HARNESS_NAMES else 99)
    return out


def recent_runs(limit=10):
    if not runs_tbl:
        return []
    try:
        items = runs_tbl.scan(Limit=60).get("Items", [])
        items.sort(key=lambda x: str(x.get("created_at", "")), reverse=True)
        return items[:limit]
    except Exception:
        return []


def list_executions():
    """RUNNING executions first, then recent terminal ones."""
    out, seen = [], set()
    try:
        for status in (["RUNNING"], [None]):
            kw = {"stateMachineArn": SM_ARN, "maxResults": 10}
            if status[0]:
                kw["statusFilter"] = status[0]
            for e in sfn.list_executions(**kw).get("executions", []):
                if e["executionArn"] in seen:
                    continue
                seen.add(e["executionArn"])
                out.append({"name": e["name"], "arn": e["executionArn"], "status": e["status"],
                            "startDate": str(e.get("startDate", "")),
                            "stopDate": str(e.get("stopDate", ""))})
    except Exception as e:
        return {"error": str(e)[:200]}
    return out


def overview():
    return {"harnesses": list_fleet(), "runs": recent_runs(10), "executions": list_executions()}


# ── /api/pipeline: SFN execution history → logical stage flow ─────────────────
STAGE_FLOW = [
    ("data-prep-generate", "Data Prep · Generate"),
    ("data-prep-curate", "Data Prep · Curate"),
    ("finetune-launch", "Finetune · Launch"),
    ("finetune-analyze", "Finetune · Analyze"),
    ("eval-gate", "Eval Gate"),
    ("deploy", "Deploy"),
    ("smoke", "Smoke Test"),
    ("teardown", "Teardown"),
    ("complete", "Complete"),
]
# SFN state name -> logical stage (same mapping pattern as the source repo's
# GitHub step-name -> stage table). RemediateFinetune re-enters finetune-launch.
STATE_TO_STAGE = {
    "DataPrepGenerate": "data-prep-generate", "DataPrepCurate": "data-prep-curate",
    "FinetuneLaunch": "finetune-launch", "FinetuneAnalyze": "finetune-analyze",
    "RemediateFinetune": "finetune-launch", "EvalGate": "eval-gate",
    "Deploy": "deploy", "SmokeTest": "smoke", "Teardown": "teardown",
    "Complete": "complete",
}
TERMINAL_FAIL_STATES = {"EscalateFail", "Fail"}


def _exec_arn(execution):
    if execution.startswith("arn:"):
        return execution
    return f"arn:aws:states:{REGION}:{ACCOUNT_ID}:execution:{STATE_MACHINE}:{execution}"


def pipeline_detail(execution=None):
    if not execution:
        exs = list_executions()
        if isinstance(exs, dict) or not exs:
            return {"execution": None, "stages": [{"key": k, "label": l, "status": "pending"}
                                                  for k, l in STAGE_FLOW], "iteration": 0}
        execution = exs[0]["arn"]
    arn = _exec_arn(execution)
    d = sfn.describe_execution(executionArn=arn)
    exec_status = d["status"]  # RUNNING | SUCCEEDED | FAILED | TIMED_OUT | ABORTED
    run_id = ""
    try:
        run_id = json.loads(d.get("input", "{}")).get("run_id", "")
    except Exception:
        pass

    entered, exited = {}, {}  # logical stage -> counts
    iteration, escalated, last_entered = 0, False, None
    token = None
    while True:
        kw = {"executionArn": arn, "maxResults": 1000}
        if token:
            kw["nextToken"] = token
        h = sfn.get_execution_history(**kw)
        for ev in h.get("events", []):
            det = ev.get("stateEnteredEventDetails")
            if det:
                name = det.get("name", "")
                if name == "IncrementIteration":
                    iteration += 1
                if name in TERMINAL_FAIL_STATES:
                    escalated = True
                st = STATE_TO_STAGE.get(name)
                if st:
                    entered[st] = entered.get(st, 0) + 1
                    last_entered = st
            det = ev.get("stateExitedEventDetails")
            if det:
                st = STATE_TO_STAGE.get(det.get("name", ""))
                if st:
                    exited[st] = exited.get(st, 0) + 1
        token = h.get("nextToken")
        if not token:
            break

    terminal = exec_status != "RUNNING"
    stages = []
    for key, label in STAGE_FLOW:
        n_in, n_out = entered.get(key, 0), exited.get(key, 0)
        if n_in == 0:
            status = "skipped" if terminal else "pending"
        elif n_out >= n_in:
            status = "succeeded"
        elif not terminal:
            status = "running"
        else:  # entered but never exited on a terminal execution
            status = "failed"
        stages.append({"key": key, "label": label, "status": status})
    if terminal and exec_status != "SUCCEEDED" and last_entered:
        for st in stages:  # make the failure location explicit even if the state "exited" into Fail
            if st["key"] == last_entered and st["status"] not in ("running",):
                st["status"] = "failed"
    return {"execution": {"arn": arn, "name": arn.rsplit(":", 1)[-1], "status": exec_status,
                          "startDate": str(d.get("startDate", "")),
                          "stopDate": str(d.get("stopDate", ""))},
            "runId": run_id, "stages": stages, "iteration": iteration,
            "escalated": escalated, "terminal": exec_status if terminal else None}


# ── /api/run: manifest + stage events + training job + gate verdict ──────────
def run_detail(run_id):
    out = {"runId": run_id, "manifest": None, "events": [], "gates": [],
           "gateVerdict": None, "trainingJob": None}
    if not run_id:
        return {"error": "run_id required"}
    try:
        obj = s3.get_object(Bucket=data_bucket(), Key=f"runs/{run_id}/manifest.json")
        out["manifest"] = json.loads(obj["Body"].read())
    except Exception as e:
        out["manifestError"] = str(e)[:150]
    if events_tbl:
        try:
            r = events_tbl.query(KeyConditionExpression=Key("run_id").eq(run_id), Limit=100)
            out["events"] = r.get("Items", [])
        except Exception as e:
            out["eventsError"] = str(e)[:150]
    # gate verdict: thresholds from params.gates vs eval stage metrics
    man = out["manifest"] or {}
    gates_cfg = (man.get("params") or {}).get("gates") or {}
    eval_metrics = ((man.get("stages") or {}).get("eval") or {}).get("metrics") or {}
    for gname, threshold in gates_cfg.items():
        actual = eval_metrics.get(gname)
        passed = None
        try:
            if actual is not None:
                passed = float(actual) >= float(threshold)
        except Exception:
            pass
        out["gates"].append({"name": gname, "threshold": threshold,
                             "actual": actual, "passed": passed})
    gp = eval_metrics.get("gate_passed")
    out["gateVerdict"] = ("passed" if gp is True else "failed" if gp is False else None)
    # training job (tolerate missing): job_name lives on the runs-table item
    job_name = ""
    if runs_tbl:
        try:
            job_name = str(runs_tbl.get_item(Key={"run_id": run_id}).get("Item", {}).get("job_name", ""))
        except Exception:
            pass
    if job_name:
        try:
            tj = sm.describe_training_job(TrainingJobName=job_name)
            out["trainingJob"] = {"name": job_name, "status": tj.get("TrainingJobStatus"),
                                  "secondaryStatus": tj.get("SecondaryStatus"),
                                  "failureReason": (tj.get("FailureReason") or "")[:200]}
        except Exception as e:
            out["trainingJob"] = {"name": job_name, "status": "UNKNOWN", "error": str(e)[:120]}
    return out


# ── observability (ported; watched = all 6 runtimes; + SageMaker tile) ───────
def observability(hours=24):
    """Per-harness runtime metrics from the AWS/Bedrock-AgentCore service namespace, plus
    account-level gen_ai token usage EMF metrics, plus a SageMaker jobs/endpoints tile."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=int(hours))
    watched = set(WATCHED_RUNTIMES)
    runtimes = {r["agentRuntimeName"]: r for r in ctl.list_agent_runtimes().get("agentRuntimes", [])
                if r["agentRuntimeName"] in watched}
    queries, order = [], []
    for i, (rname, r) in enumerate(runtimes.items()):
        arn = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:runtime/{r['agentRuntimeId']}"
        dims = [{"Name": "Name", "Value": f"{rname}::DEFAULT"},
                {"Name": "Operation", "Value": "InvokeAgentRuntime"},
                {"Name": "Resource", "Value": arn}]
        for j, (metric, stat) in enumerate([("Invocations", "Sum"), ("Latency", "Average"),
                                            ("Sessions", "Sum"), ("SystemErrors", "Sum"),
                                            ("UserErrors", "Sum"), ("Throttles", "Sum")]):
            queries.append({"Id": f"m{i}_{j}", "MetricStat": {
                "Metric": {"Namespace": "AWS/Bedrock-AgentCore", "MetricName": metric, "Dimensions": dims},
                "Period": 3600 * int(hours), "Stat": stat}, "ReturnData": True})
            order.append((rname, metric, stat))
    # token usage: EMF gen_ai.client.token.usage — dims vary, list then sum input/output
    tok_metrics = cw.list_metrics(Namespace="bedrock-agentcore",
                                  MetricName="gen_ai.client.token.usage").get("Metrics", [])
    for k, m in enumerate(tok_metrics):
        ttype = next((d["Value"] for d in m["Dimensions"] if d["Name"] == "gen_ai.token.type"), "?")
        queries.append({"Id": f"t{k}", "MetricStat": {
            "Metric": m, "Period": 3600 * int(hours), "Stat": "Sum"}, "ReturnData": True})
        order.append(("_tokens", ttype, "Sum"))
    out = {rn: {} for rn in runtimes}
    tokens = {"input": 0, "output": 0}
    if queries:
        res = cw.get_metric_data(MetricDataQueries=queries, StartTime=start, EndTime=end)
        for q, (owner, metric, stat) in zip(res["MetricDataResults"], order):
            val = sum(q["Values"]) if stat == "Sum" else (sum(q["Values"]) / len(q["Values"]) if q["Values"] else 0)
            if owner == "_tokens":
                tokens[metric] = tokens.get(metric, 0) + val
            else:
                out[owner][metric] = round(val, 1)
    for rn in out:
        inv = out[rn].get("Invocations", 0)
        errs = out[rn].get("SystemErrors", 0) + out[rn].get("UserErrors", 0)
        out[rn]["ErrorRatePct"] = round(100.0 * errs / inv, 1) if inv else 0.0
    # daily invocation series for the trend chart (1 bucket/day)
    dq, dorder = [], []
    for i, (rname, r) in enumerate(runtimes.items()):
        arn = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:runtime/{r['agentRuntimeId']}"
        dims = [{"Name": "Name", "Value": f"{rname}::DEFAULT"},
                {"Name": "Operation", "Value": "InvokeAgentRuntime"},
                {"Name": "Resource", "Value": arn}]
        dq.append({"Id": f"d{i}", "MetricStat": {
            "Metric": {"Namespace": "AWS/Bedrock-AgentCore", "MetricName": "Invocations",
                       "Dimensions": dims}, "Period": 86400, "Stat": "Sum"}, "ReturnData": True})
        dorder.append(rname)
    if dq:
        dres = cw.get_metric_data(MetricDataQueries=dq, StartTime=start, EndTime=end,
                                  ScanBy="TimestampAscending")
        for q, rname in zip(dres["MetricDataResults"], dorder):
            out[rname]["daily"] = [{"d": t.strftime("%m-%d"), "v": v}
                                   for t, v in zip(q["Timestamps"], q["Values"])]
    # SageMaker tile: last 10 llmops- training jobs + llmops-student endpoints
    sagemaker = {"trainingJobs": [], "endpoints": []}
    try:
        for tj in sm.list_training_jobs(NameContains="llmops-", SortBy="CreationTime",
                                        SortOrder="Descending", MaxResults=10).get("TrainingJobSummaries", []):
            sagemaker["trainingJobs"].append({"name": tj["TrainingJobName"],
                                              "status": tj["TrainingJobStatus"],
                                              "createdAt": str(tj.get("CreationTime", ""))})
    except Exception as e:
        sagemaker["trainingJobsError"] = str(e)[:150]
    try:
        for ep in sm.list_endpoints(NameContains="llmops-student", SortBy="CreationTime",
                                    SortOrder="Descending", MaxResults=10).get("Endpoints", []):
            sagemaker["endpoints"].append({"name": ep["EndpointName"],
                                           "status": ep["EndpointStatus"],
                                           "createdAt": str(ep.get("CreationTime", ""))})
    except Exception as e:
        sagemaker["endpointsError"] = str(e)[:150]
    return {"windowHours": int(hours), "harnesses": out,
            "tokens": {k: int(v) for k, v in tokens.items()},
            "tokenScope": "account-wide agent runtimes (EMF gen_ai.client.token.usage)",
            "sagemaker": sagemaker,
            "source": "AWS/Bedrock-AgentCore service metrics + DEFAULT log group EMF + SageMaker"}


# ── evaluations (ported unchanged: all configs, tolerant score extraction) ────
def evaluations():
    """Online evaluation configs + recent scores from the results log group."""
    cfgs = ctl.list_online_evaluation_configs().get("onlineEvaluationConfigs", [])
    out = []
    for c0 in cfgs:
        cid = c0.get("onlineEvaluationConfigId")
        item = {"id": cid, "name": c0.get("onlineEvaluationConfigName"),
                "status": str(c0.get("status")), "executionStatus": str(c0.get("executionStatus", ""))}
        try:
            d = ctl.get_online_evaluation_config(onlineEvaluationConfigId=cid)
            cfg = d.get("onlineEvaluationConfig", d)
            item["evaluators"] = [e.get("evaluatorId") for e in cfg.get("evaluators", [])]
            item["insights"] = [i.get("insightId", "").replace("Builtin.Insight.", "")
                                for i in cfg.get("insights", [])]
            item["frequencies"] = (cfg.get("clusteringConfig") or {}).get("frequencies", [])
            item["sampling"] = cfg.get("rule", {}).get("samplingConfig", {}).get("samplingPercentage")
            item["logGroups"] = cfg.get("dataSourceConfig", {}).get("cloudWatchLogs", {}).get("logGroupNames", [])
            item["status"] = str(cfg.get("status", item["status"]))
        except Exception as e:
            item["detailError"] = str(e)[:150]
        # recent scores from results log group (honest: empty until evaluator has scored traffic)
        scores = []
        try:
            ev = logsc.filter_log_events(logGroupName=EVAL_RESULTS_LG_PREFIX + cid, limit=50)
            for e0 in ev.get("events", []):
                try:
                    scores.append(json.loads(e0["message"]))
                except Exception:
                    pass
            if not scores:
                item["scoresNote"] = "evaluator ACTIVE — awaiting scored traffic (runs on next harness invocation)"
        except logsc.exceptions.ResourceNotFoundException:
            item["scoresNote"] = "no results log group yet — evaluator has not scored any traffic"
        except Exception as e:
            item["scoresNote"] = f"results read error: {str(e)[:120]}"
        item["recentScores"] = scores[-20:]
        # aggregate per evaluator — tolerant extraction, the preview result schema may drift
        agg = {}
        for j in scores:
            att = j.get("attributes") or {}
            name = (att.get("gen_ai.evaluation.name") or j.get("evaluatorId") or j.get("evaluator")
                    or j.get("evaluatorName") or j.get("metricName") or "")
            if name == "gen_ai.evaluation.result":
                name = att.get("gen_ai.evaluation.name", "")
            val = att.get("gen_ai.evaluation.score.value")
            val = float(val) if isinstance(val, (int, float)) else None
            for k in ("score", "value", "result", "metricValue"):
                if val is not None:
                    break
                v = j.get(k)
                if isinstance(v, (int, float)):
                    val = float(v)
                    break
                if isinstance(v, dict):
                    for kk in ("value", "score"):
                        if isinstance(v.get(kk), (int, float)):
                            val = float(v[kk])
                            break
                if val is not None:
                    break
            if name and val is not None:
                a = agg.setdefault(str(name).replace("Builtin.", ""), {"n": 0, "sum": 0.0})
                a["n"] += 1
                a["sum"] += val
        item["scoreStats"] = {k: {"count": v["n"], "avg": round(v["sum"] / v["n"], 3)}
                              for k, v in agg.items() if v["n"]}
        out.append(item)
    return {"configs": out,
            "note": "Online evaluations score live harness traces (preview). Scores also surface in "
                    "CloudWatch → AgentCore Observability."}


# ── batch evaluations + insights (DATA-plane ops, ported) ─────────────────────
# Session-id source differs from the source repo: the pipeline's session ids are
# deterministic <run_id>-<stage>-<task>, so we reconstruct them from recent run ids.
STAGE_TASKS = [("data-prep", "generate"), ("data-prep", "curate"),
               ("finetune", "launch"), ("finetune", "analyze"), ("finetune", "remediate"),
               ("eval", "gate"), ("deploy", "deploy"), ("deploy", "smoke"), ("deploy", "teardown")]


def _recent_session_ids(cap=20, stage_filter=None):
    """Session ids for stage/tasks that ACTUALLY RAN, reconstructed from the
    stage-events table (fabricating ids for never-executed stage/task combos
    just produces 'failed sessions' — no spans exist for them).

    stage_filter: sessions live on the harness that RAN them, so batch-eval
    fan-out sends each runtime only its own stage's session ids."""
    sids = []
    for it in recent_runs(30):
        rid = str(it.get("run_id", ""))
        if not rid:
            continue
        if SPANS_SINCE and str(it.get("created_at", "")) < SPANS_SINCE:
            continue
        ran = set()
        try:
            for ev0 in events_tbl.query(KeyConditionExpression=Key("run_id").eq(rid)).get("Items", []):
                stage = str(ev0.get("sk", "")).split("#")[1] if "#" in str(ev0.get("sk", "")) else ""
                task = ""
                try:
                    task = json.loads(ev0.get("detail", "{}")).get("task", "")
                except Exception:
                    pass
                if stage:
                    ran.add((stage, task))
        except Exception:
            pass
        for stage, task in STAGE_TASKS:
            if stage_filter and stage != stage_filter:
                continue
            # include only combos this run actually executed (task match when known)
            if ran and not any(s == stage and (t == task or not t) for s, t in ran):
                continue
            sids.append(session_id(rid, stage, task))
            if len(sids) >= cap:
                return sids
    return sids


def _pipeline_runtimes():
    """The five pipeline-stage runtimes (orchestrator sessions never match the
    reconstructed pipeline session ids, so it is excluded from batch scoring)."""
    names = [n for n in WATCHED_RUNTIMES if "orchestrator" not in n]
    out = []
    for r in ctl.list_agent_runtimes().get("agentRuntimes", []):
        if r["agentRuntimeName"] in names:
            out.append({"name": r["agentRuntimeName"],
                        "lg": f"/aws/bedrock-agentcore/runtimes/{r['agentRuntimeId']}-DEFAULT",
                        "svc": f"{r['agentRuntimeName']}.DEFAULT"})
    return out


def _eval_data_source_for(rt, sids):
    """One runtime per call — live-verified API constraint: serviceNames caps at
    ONE entry (and logGroupNames at five), so multi-runtime scoring fans out."""
    return {"cloudWatchLogs": {"serviceNames": [rt["svc"]], "logGroupNames": [rt["lg"]],
                               "filterConfig": {"sessionIds": sids}}}


def start_batch_eval():
    """Fan out one batch evaluation per pipeline runtime (serviceNames caps at 1)."""
    started, errors = [], []
    for rt in _pipeline_runtimes():
        stage = rt["name"].replace("harness_llmops_", "")[:12]
        ddb_stage = stage.replace("_", "-")  # runtime names use _, stage names use -
        sids = _recent_session_ids(cap=20, stage_filter=ddb_stage)
        if not sids:
            continue  # no sessions for this stage in the window
        try:
            r = data.start_batch_evaluation(
                batchEvaluationName=f"llmops_be_{stage}_{secrets.token_hex(3)}",  # [a-zA-Z][a-zA-Z0-9_]{0,47}
                evaluators=[{"evaluatorId": "Builtin.Correctness"},
                            {"evaluatorId": "Builtin.GoalSuccessRate"},
                            {"evaluatorId": "Builtin.ToolSelectionAccuracy"}],
                dataSourceConfig=_eval_data_source_for(rt, sids),
                clientToken=secrets.token_hex(20),
                description=f"LLMOps admin dashboard — {stage} stage sessions")
            started.append({"stage": stage, "id": r.get("batchEvaluationId"),
                            "status": str(r.get("status"))})
        except Exception as e:  # collect per-runtime failures, don't abort the fan-out
            errors.append({"stage": stage, "error": str(e)[:200]})
    if not started and not errors:
        return {"error": "no recent pipeline runs after SPANS_SINCE to score — start a run first"}
    return {"started": started, "errors": errors}


def list_batch_evaluations():
    """Batch evaluations via the DATA-plane SDK. Returns {'error':...} on upstream
    failure so the caller can tell 'API down' apart from 'none yet'."""
    try:
        out = []
        for be in data.list_batch_evaluations().get("batchEvaluations", [])[:10]:
            if str(be.get("batchEvaluationName", "")).startswith("llmops_ins_"):
                continue  # insights reports render in their own panel
            item = {"id": be.get("batchEvaluationId"), "name": be.get("batchEvaluationName"),
                    "status": str(be.get("status")), "createdAt": str(be.get("createdAt", ""))}
            try:
                d = data.get_batch_evaluation(batchEvaluationId=item["id"])
                bd = d.get("batchEvaluation", d)
                res = bd.get("evaluationResults", {})
                item["sessions"] = {"total": res.get("totalNumberOfSessions", 0),
                                    "completed": res.get("numberOfSessionsCompleted", 0),
                                    "failed": res.get("numberOfSessionsFailed", 0)}
                item["evaluatorSummaries"] = [
                    {"evaluator": e0.get("evaluatorId", "").replace("Builtin.", ""),
                     "avg": (e0.get("statistics") or {}).get("averageScore"),
                     "evaluated": e0.get("totalEvaluated", 0), "failed": e0.get("totalFailed", 0)}
                    for e0 in res.get("evaluatorSummaries", [])]
            except Exception:
                pass
            out.append(item)
        out.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
        return out
    except Exception as e:
        return {"error": str(e)[:200]}


def start_insights_report():
    """Fan out one insights report per pipeline runtime (serviceNames caps at 1)."""
    started, errors = [], []
    # Service quota: max 5 CONCURRENT batch evaluations account-wide — insights
    # runs per-stage too, so cap the fan-out at the two most informative stages
    # when others are running (data-prep + finetune carry most failure signal).
    for rt in _pipeline_runtimes():
        stage = rt["name"].replace("harness_llmops_", "")[:12]
        ddb_stage = stage.replace("_", "-")
        sids = _recent_session_ids(cap=20, stage_filter=ddb_stage)
        if not sids:
            continue
        try:
            r = data.start_batch_evaluation(
                batchEvaluationName=f"llmops_ins_{stage}_{secrets.token_hex(3)}",
                insights=[{"insightId": "Builtin.Insight.FailureAnalysis"},
                          {"insightId": "Builtin.Insight.UserIntent"},
                          {"insightId": "Builtin.Insight.ExecutionSummary"}],
                dataSourceConfig=_eval_data_source_for(rt, sids),
                clientToken=secrets.token_hex(20),
                description=f"LLMOps admin dashboard insights — {stage} stage")
            started.append({"stage": stage, "id": r.get("batchEvaluationId"),
                            "status": str(r.get("status"))})
        except Exception as e:
            errors.append({"stage": stage, "error": str(e)[:200]})
    if not started and not errors:
        return {"error": "no recent pipeline runs after SPANS_SINCE to analyze — start a run first"}
    return {"started": started, "errors": errors}


def get_insights_report(bid):
    d = data.get_batch_evaluation(batchEvaluationId=bid)
    be = d.get("batchEvaluation", d)
    return {"id": bid, "status": str(be.get("status")),
            "failures": (be.get("failureAnalysisResult") or {}).get("failures", []),
            "intents": (be.get("userIntentResult") or {}).get("userIntents", []),
            "summaries": (be.get("executionSummaryResult") or {}).get("executionSummaries", [])}


def list_insights_reports():
    out = []
    try:
        for be in data.list_batch_evaluations().get("batchEvaluations", []):
            if str(be.get("batchEvaluationName", "")).startswith("llmops_ins_"):
                out.append({"id": be.get("batchEvaluationId"), "name": be.get("batchEvaluationName"),
                            "status": str(be.get("status")), "createdAt": str(be.get("createdAt", ""))})
        out.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
    except Exception:
        pass
    return out[:5]


# ── optimizations: AWS-native recommendations + Bedrock-drafted prompts ───────
def _runtime_for(harness_name):
    return next((r for r in ctl.list_agent_runtimes().get("agentRuntimes", [])
                 if r["agentRuntimeName"] == f"harness_{harness_name}"), None)


def list_native_recommendations():
    """AWS-native Optimizations Recommendations (data-plane SDK)."""
    try:
        out = []
        for rec in data.list_recommendations().get("recommendationSummaries", [])[:10]:
            item = {"id": rec.get("recommendationId"), "name": rec.get("name"),
                    "status": str(rec.get("status")), "type": rec.get("type"),
                    "createdAt": str(rec.get("createdAt", ""))}
            if item["status"] == "COMPLETED":
                try:
                    d = data.get_recommendation(recommendationId=item["id"])
                    rr = d.get("recommendation", d).get("recommendationResult", {})
                    spr = rr.get("systemPromptRecommendationResult", {})
                    item["recommendedPrompt"] = spr.get("recommendedSystemPrompt", "")
                    item["explanation"] = (spr.get("explanation") or "")[:400]
                except Exception:
                    pass
            out.append(item)
        out.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
        return out
    except Exception as e:
        return {"error": str(e)[:200]}


def start_native_recommendation(harness=None):
    """AWS-native system-prompt recommendation from the last 12h of the target
    harness's traces. Enum SYSTEM_PROMPT_RECOMMENDATION; max ONE evaluator."""
    hname = harness or OPTIMIZE_HARNESS
    hid = _resolve_harness_id(hname)
    h = ctl.get_harness(harnessId=hid)["harness"]
    hname = h.get("harnessName", hname)
    cur = (h.get("systemPrompt") or [{}])[0].get("text", "")
    rt = _runtime_for(hname)
    if not rt:
        return {"error": f"no runtime harness_{hname} found"}
    lg_arn = (f"arn:aws:logs:{REGION}:{ACCOUNT_ID}:log-group:"
              f"/aws/bedrock-agentcore/runtimes/{rt['agentRuntimeId']}-DEFAULT")
    end = datetime.now(timezone.utc)
    r = data.start_recommendation(
        name="llmops_rec_" + secrets.token_hex(3),
        type="SYSTEM_PROMPT_RECOMMENDATION",     # enum, not "SYSTEM_PROMPT"
        recommendationConfig={"systemPromptRecommendationConfig": {
            "systemPrompt": {"text": cur},
            "agentTraces": {"cloudwatchLogs": {
                "logGroupArns": [lg_arn],
                "serviceNames": [f"harness_{hname}.DEFAULT"],
                "startTime": end - timedelta(hours=12), "endTime": end}},
            "evaluationConfig": {"evaluators": [   # max ONE evaluator
                {"evaluatorArn": "arn:aws:bedrock-agentcore:::evaluator/Builtin.GoalSuccessRate"}]}}},
        clientToken=secrets.token_hex(20))
    return {"id": r.get("recommendationId"), "status": str(r.get("status")), "harness": hname}


def apply_native_recommendation(rec_id, harness=None):
    d = data.get_recommendation(recommendationId=rec_id)
    rec = d.get("recommendation", d)
    spr = rec.get("recommendationResult", {}).get("systemPromptRecommendationResult", {})
    prompt = spr.get("recommendedSystemPrompt", "")
    if not prompt:
        return {"error": "recommendation has no completed prompt"}
    hid = _resolve_harness_id(harness or OPTIMIZE_HARNESS)
    ctl.update_harness(harnessId=hid, systemPrompt=[{"text": prompt}],
                       clientToken=secrets.token_hex(20))  # clientToken >= 33 chars
    return {"ok": True, "applied": rec_id, "harnessId": hid}


def list_optimizations():
    if not console_tbl:
        return []
    try:
        items = [i for i in console_tbl.scan(Limit=50).get("Items", [])
                 if str(i.get("id", "")).startswith("opt-")]
        items.sort(key=lambda x: x.get("startedAt", ""), reverse=True)
        return items[:10]
    except Exception:
        return []


def enqueue_optimization(now_iso, harness=None):
    """API Gateway caps integrations at 30s; Bedrock generation takes longer.
    Enqueue a placeholder and self-invoke async (ported pattern)."""
    opt_id = "opt-" + secrets.token_hex(5)
    hid = _resolve_harness_id(harness or OPTIMIZE_HARNESS)
    if console_tbl:
        console_tbl.put_item(Item={"id": opt_id, "startedAt": now_iso,
                                   "status": "generating", "harnessId": hid})
    if SELF_FUNCTION:
        lam.invoke(FunctionName=SELF_FUNCTION, InvocationType="Event",
                   Payload=json.dumps({"mode": "optimize", "opt_id": opt_id,
                                       "harness_id": hid, "now_iso": now_iso}).encode())
    return {"id": opt_id, "status": "generating",
            "note": "generation runs async (~30s); refresh the Optimizations panel"}


def generate_optimization(now_iso, opt_id=None, harness_id=None):
    """Draft an improved system prompt via Bedrock from the harness's current prompt,
    recent run outcomes and eval scores; store for human review; apply via UpdateHarness."""
    hid = harness_id or _resolve_harness_id(OPTIMIZE_HARNESS)
    h = ctl.get_harness(harnessId=hid)["harness"]
    cur = h.get("systemPrompt") or []
    cur_text = cur[0].get("text", "") if cur else ""
    # recent run outcomes: status/current_stage + latest run's gate metrics
    outcomes = [{"run_id": str(i.get("run_id", "")), "status": str(i.get("status", "")),
                 "stage": str(i.get("current_stage", ""))} for i in recent_runs(5)]
    gate_metrics = {}
    if outcomes:
        try:
            latest = run_detail(outcomes[0]["run_id"])
            gate_metrics = {"gates": latest.get("gates", []), "verdict": latest.get("gateVerdict")}
        except Exception:
            pass
    evs = evaluations()["configs"]
    ev_summary = "; ".join(
        f"{e['name']}={e['status']} stats={json.dumps(e.get('scoreStats', {}), default=str)}"
        for e in evs) or "none"
    prompt = (
        "You are optimizing the system prompt of an agent in an LLMOps fine-tuning pipeline "
        "(Bedrock AgentCore harness). The pipeline distills a teacher model into a small student "
        "via QLoRA on SageMaker, gates on eval metrics, and deploys behind a smoke test.\n"
        f"HARNESS: {h.get('harnessName', hid)}\n"
        f"CURRENT SYSTEM PROMPT:\n{cur_text}\n\n"
        f"RECENT RUN OUTCOMES (JSON): {json.dumps(outcomes, default=str)[:2000]}\n"
        f"LATEST GATE METRICS (JSON): {json.dumps(gate_metrics, default=str)[:1500]}\n"
        f"ONLINE EVALUATIONS: {ev_summary[:1500]}\n\n"
        "Propose ONE improved system prompt that keeps current strengths, makes the stage_complete "
        "reporting contract explicit (outputs, metrics, evidence with fixed field names), and "
        "addresses whatever the run outcomes and eval scores show is weakest. Reply as JSON: "
        '{"proposed_prompt": "...", "rationale": "...", "expected_improvements": ["..."]}')
    resp = brt.converse(modelId=JUDGE_MODEL,
                        messages=[{"role": "user", "content": [{"text": prompt}]}],
                        inferenceConfig={"maxTokens": 4000, "temperature": 0.2})
    txt = resp["output"]["message"]["content"][0]["text"].strip()
    # strip markdown code fences the model sometimes wraps around JSON
    if txt.startswith("```"):
        txt = txt.split("\n", 1)[1] if "\n" in txt else txt
        if txt.rstrip().endswith("```"):
            txt = txt.rstrip()[:-3]
    try:
        start = txt.index("{"); end = txt.rindex("}") + 1
        rec = json.loads(txt[start:end])
    except Exception:
        rec = {"proposed_prompt": txt, "rationale": "model returned non-JSON; raw text kept",
               "expected_improvements": []}
    item = {"id": opt_id or ("opt-" + secrets.token_hex(5)), "startedAt": now_iso,
            "status": "proposed", "harnessId": hid, "currentPrompt": cur_text,
            "proposedPrompt": rec.get("proposed_prompt", ""), "rationale": rec.get("rationale", ""),
            "expectedImprovements": rec.get("expected_improvements", []), "model": JUDGE_MODEL}
    if console_tbl:
        console_tbl.put_item(Item=item)
    return item


def apply_optimization(opt_id, now_iso):
    if not console_tbl:
        return {"error": "no console table"}
    item = console_tbl.get_item(Key={"id": opt_id}).get("Item")
    if not item or not item.get("proposedPrompt"):
        return {"error": "recommendation not found"}
    ctl.update_harness(harnessId=item["harnessId"],
                       systemPrompt=[{"text": item["proposedPrompt"]}],
                       clientToken=secrets.token_hex(20))
    console_tbl.update_item(Key={"id": opt_id},
                            UpdateExpression="SET #s = :s, appliedAt = :t",
                            ExpressionAttributeNames={"#s": "status"},
                            ExpressionAttributeValues={":s": "applied", ":t": now_iso})
    return {"ok": True, "applied": opt_id, "harnessId": item["harnessId"]}


# ── start a pipeline run ──────────────────────────────────────────────────────
def start_run(body):
    params = {}
    for k in ("task_count", "sample_count"):
        if body.get(k) not in (None, ""):
            try:
                params[k] = int(body[k])
            except Exception:
                pass
    if body.get("note"):
        params["note"] = str(body["note"])[:200]
    payload = {"trigger_source": "console", "params": params}
    r = lam.invoke(FunctionName=START_FN, InvocationType="RequestResponse",
                   Payload=json.dumps(payload).encode())
    try:
        out = json.loads(r["Payload"].read())
    except Exception:
        out = {"note": "started (no parseable response)"}
    if r.get("FunctionError"):
        return {"error": f"start-pipeline failed: {json.dumps(out, default=str)[:300]}"}
    return {"ok": True, "result": out}


# ── HTTP glue (ported: security headers, auth, response helper) ───────────────
# CSP: the dashboard is one self-contained HTML document served by this Lambda.
# 'unsafe-inline' is unavoidable today (inline <script> + onclick handlers); CSP is
# defence-in-depth, escaping at the sink (esc()/jstr() in frontend.html) is the
# primary XSS control.
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
       "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; frame-src 'self'; frame-ancestors 'self'; "
       "form-action 'self'; base-uri 'none'; object-src 'none'")

_SEC_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "SAMEORIGIN",
    "referrer-policy": "strict-origin-when-cross-origin",
    "strict-transport-security": "max-age=31536000; includeSubDomains",
    "content-security-policy": CSP,
}


def _resp(code, body, ctype="application/json"):
    headers = {"content-type": ctype}
    headers.update(_SEC_HEADERS)
    if ctype.startswith("application/json"):
        headers["cache-control"] = "no-store"
    if ALLOWED_ORIGIN:
        headers.update({"access-control-allow-origin": ALLOWED_ORIGIN,
                        "access-control-allow-methods": "GET,POST,OPTIONS",
                        "access-control-allow-headers": "content-type,authorization",
                        "vary": "origin"})
    return {"statusCode": code, "headers": headers,
            "body": body if isinstance(body, str) else json.dumps(body, default=str)}


def cognito_login(username, password):
    """Exchange username/password for a Cognito access token (USER_PASSWORD_AUTH)."""
    if not COGNITO_CLIENT_ID:
        return {"error": "Cognito not configured"}
    try:
        r = cognito.initiate_auth(ClientId=COGNITO_CLIENT_ID, AuthFlow="USER_PASSWORD_AUTH",
                                  AuthParameters={"USERNAME": username, "PASSWORD": password})
        a = r["AuthenticationResult"]
        return {"accessToken": a["AccessToken"], "expiresIn": a["ExpiresIn"],
                "refreshToken": a.get("RefreshToken", "")}
    except cognito.exceptions.NotAuthorizedException:
        return {"error": "invalid username or password"}
    except Exception as e:
        return {"error": str(e)[:200]}


def _authed(headers):
    """Cognito access token in Authorization: Bearer, validated server-side via
    GetUser (checks signature/expiry/revocation)."""
    h = {k.lower(): v for k, v in (headers or {}).items()}
    auth = h.get("authorization", "")
    if auth.startswith("Bearer ") and COGNITO_POOL_ID:
        try:
            cognito.get_user(AccessToken=auth[7:].strip())
            return True
        except Exception:
            return False
    return False


def handler(event, context):
    # Async self-invocation path (optimization draft worker)
    if isinstance(event, dict) and event.get("mode") == "optimize":
        try:
            generate_optimization(event.get("now_iso", ""), event.get("opt_id"),
                                  event.get("harness_id"))
        except Exception as e:
            if console_tbl and event.get("opt_id"):
                console_tbl.update_item(Key={"id": event["opt_id"]},
                                        UpdateExpression="SET #s = :s, error_msg = :e",
                                        ExpressionAttributeNames={"#s": "status"},
                                        ExpressionAttributeValues={":s": "error", ":e": str(e)[:300]})
        return {"ok": True}

    rc = (event.get("requestContext") or {}).get("http") or {}
    method = rc.get("method", "GET")
    path = rc.get("path", "/")
    headers = event.get("headers") or {}
    qs = event.get("queryStringParameters") or {}

    if method == "OPTIONS":
        return _resp(204, "")

    if method == "GET" and path in ("/", "/index.html"):
        return _resp(200, FRONTEND_HTML, "text/html; charset=utf-8")

    try:
        if method == "GET" and path == "/api/overview":
            return _resp(200, overview())
        if method == "GET" and path == "/api/pipeline":
            return _resp(200, pipeline_detail(qs.get("execution")))
        if method == "GET" and path == "/api/run":
            return _resp(200, run_detail(qs.get("run_id", "")))
        if method == "GET" and path == "/api/observability":
            return _resp(200, observability(qs.get("hours", 24)))
        if method == "GET" and path == "/api/evaluations":
            return _resp(200, evaluations())
        if method == "GET" and path == "/api/batch-evals":
            return _resp(200, {"batchEvaluations": list_batch_evaluations()})
        if method == "GET" and path == "/api/insights-reports":
            return _resp(200, {"reports": list_insights_reports()})
        if method == "GET" and path == "/api/insights-report":
            return _resp(200, get_insights_report(qs.get("id", "")))
        if method == "GET" and path == "/api/optimizations":
            return _resp(200, {"recommendations": list_optimizations(),
                               "native": list_native_recommendations()})

        raw = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            import base64
            raw = base64.b64decode(raw).decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except Exception:
            body = {}
        if method == "POST" and path == "/api/login":
            return _resp(200, cognito_login(str(body.get("username", "")), str(body.get("password", ""))))
        if method == "POST":
            if not _authed(headers):
                return _resp(401, {"error": "unauthorized"})
            now = datetime.now(timezone.utc).isoformat()
            if path == "/api/start-run":
                return _resp(200, start_run(body))
            if path == "/api/batch-eval":
                return _resp(200, start_batch_eval())
            if path == "/api/insights-report":
                return _resp(200, start_insights_report())
            if path == "/api/native-rec":
                return _resp(200, start_native_recommendation(body.get("harness")))
            if path == "/api/native-rec-apply":
                return _resp(200, apply_native_recommendation(body.get("id", ""), body.get("harness")))
            if path == "/api/optimize":
                return _resp(200, enqueue_optimization(now, body.get("harness")))
            if path == "/api/optimize-apply":
                return _resp(200, apply_optimization(body.get("id", ""), now))
        return _resp(404, {"error": "not found", "path": path})
    except Exception as e:
        return _resp(500, {"error": str(e)})
