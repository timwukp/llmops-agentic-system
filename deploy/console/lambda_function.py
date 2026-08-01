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
import time
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Key

REGION = os.environ.get("AWS_REGION", "us-east-1")
ACCOUNT_ID = os.environ.get("ACCOUNT_ID") or boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]

CONSOLE_TABLE = os.environ.get("CONSOLE_TABLE", "LlmopsAdminRuns")   # optimization drafts (opt- prefix)
RUNS_TABLE = os.environ.get("RUNS_TABLE", "llmops-pipeline-runs")    # pipeline runs (PK run_id)
EVENTS_TABLE = os.environ.get("EVENTS_TABLE", "llmops-stage-events") # stage events (PK run_id, SK sk)
ESTIMATES_TABLE = os.environ.get("ESTIMATES_TABLE", "llmops-cost-estimates")  # PK id
ACTUALS_TABLE = os.environ.get("ACTUALS_TABLE", "llmops-cost-actuals")        # PK project, SK sk
FINOPS_FN = os.environ.get("FINOPS_FN", "llmops-finops-reconcile")
PROJECT = os.environ.get("PROJECT", "llmops-agentic-system")
#: Approval fires when EITHER this run's worst case exceeds the single-run limit, OR
#: project-to-date actual + this estimate exceeds the cumulative one. Two independent
#: limits, because a stream of $500 runs is the same $2000 exposure as one $2000 run.
APPROVAL_LIMIT_USD = float(os.environ.get("APPROVAL_LIMIT_USD", "2000"))
CUMULATIVE_LIMIT_USD = float(os.environ.get("CUMULATIVE_LIMIT_USD", "2000"))
#: Cognito group whose members may approve. Membership is checked server-side on every
#: approval call; hiding the button in the UI is not a control.
APPROVER_GROUP = os.environ.get("APPROVER_GROUP", "llmops-approver")
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
estimates_tbl = _ddb.Table(ESTIMATES_TABLE) if ESTIMATES_TABLE else None
actuals_tbl = _ddb.Table(ACTUALS_TABLE) if ACTUALS_TABLE else None

EVAL_RESULTS_LG_PREFIX = "/aws/bedrock-agentcore/evaluations/results/"

# The 7 harnesses. Runtime name = "harness_" + harnessName. llmops_finops is the audit
# runtime: it sits beside llmops_orchestrator above the state machine rather than inside
# it, so it appears in the fleet view but never in a run's stage sequence.
HARNESS_NAMES = ["llmops_data_prep", "llmops_finetune", "llmops_eval",
                 "llmops_deploy", "llmops_monitor", "llmops_orchestrator",
                 "llmops_finops"]
WATCHED_RUNTIMES = [f"harness_{n}" for n in HARNESS_NAMES]

# ── frontend: read ONCE at cold start (replaces the source repo's inline string) ─
_HERE = os.path.dirname(os.path.abspath(__file__))
try:
    with open(os.path.join(_HERE, "frontend.html"), encoding="utf-8") as _f:
        FRONTEND_HTML = _f.read()
except Exception as _e:  # zip built without frontend.html — fail visibly, not blank
    FRONTEND_HTML = f"<h1>frontend.html missing from bundle: {_e}</h1>"

# ── architecture diagrams: bundled in the zip and served same-origin, because the
# CSP is connect-src 'self' — the Architecture tab fetches these instead of GitHub ─
ARCH_SVGS = {}
for _name in ("architecture-high-level.svg", "architecture-low-level.svg",
              "architecture-console.svg"):
    try:
        with open(os.path.join(_HERE, _name), encoding="utf-8") as _f:
            ARCH_SVGS[f"/docs/{_name}"] = _f.read()
    except Exception as _e:  # missing from the bundle — 404 with the reason, not a blank panel
        ARCH_SVGS[f"/docs/{_name}"] = None

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
    cfgs = [c for c in ctl.list_online_evaluation_configs().get("onlineEvaluationConfigs", [])
            if str(c.get("onlineEvaluationConfigName", "")).startswith("llmops_")]
    # telemetry isolation: ui_qa_* configs belong to the agent-cicd-admin (CI/CD)
    # dashboard — showing them here conflates two different platforms' scores
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
        # recent scores from results log group (honest: empty until evaluator has scored traffic).
        # filter_log_events without startTime can return an empty page + nextToken even when
        # events exist — pass an explicit window AND follow the token (bounded) or scores
        # that are really there render as "awaiting traffic".
        scores = []
        try:
            now_ms = int(time.time() * 1000)
            kw = {"logGroupName": EVAL_RESULTS_LG_PREFIX + cid, "limit": 50,
                  "startTime": now_ms - 14 * 24 * 3600 * 1000, "endTime": now_ms + 60_000}
            for _ in range(10):  # bounded pagination
                ev = logsc.filter_log_events(**kw)
                for e0 in ev.get("events", []):
                    try:
                        scores.append(json.loads(e0["message"]))
                    except Exception:
                        pass
                tok = ev.get("nextToken")
                if not tok or len(scores) >= 50:
                    break
                kw["nextToken"] = tok
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
        evals = [b for b in data.list_batch_evaluations().get("batchEvaluations", [])
                 if str(b.get("batchEvaluationName", "")).startswith("llmops_be_")]
        # llmops_be_ prefix isolates this platform's scores (ui_qa_* = CI/CD stack)
        # and excludes llmops_ins_* (insights render in their own panel)
        for be in evals[:10]:
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
        recs = [r for r in data.list_recommendations().get("recommendationSummaries", [])
                if str(r.get("name", "")).startswith("llmops_")]  # isolate from ui_qa_* (CI/CD stack)
        for rec in recs[:10]:
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

    # ── the cost gate ────────────────────────────────────────────────────────
    # An estimate_id is optional: every run before this feature launched without one,
    # and keeping that path legal is what lets the variance report state honestly what
    # fraction of spend was never estimated. But when one IS supplied, it is the gate.
    est = None
    if body.get("estimate_id"):
        est = _get_estimate(body["estimate_id"])
        if not est:
            return {"error": "unknown estimate_id", "status_code": 400}
        status = est.get("status")
        try:
            gate = json.loads(est.get("gate", "{}"))
        except Exception:
            gate = {}
        # Re-derive the gate at launch time. The estimate may have been priced when
        # project-to-date was low; launching it a week later is a different exposure,
        # and a gate that trusts a stale verdict is no gate.
        fresh = gate_decision(_f(est.get("worst_case_usd")))
        needs_approval = bool(gate.get("approval_required")
                              or fresh.get("approval_required"))
        cm = _cost_model()
        if needs_approval:
            # Delegated so the console and the pipeline agree on what "may launch"
            # means; a second copy of this rule is a copy that can drift.
            ok = cm.can_launch(est) if cm else {
                "ok": False, "code": 503,
                "error": "cost model unavailable — cannot verify the approval status"}
            if not ok.get("ok"):
                return {"error": (ok.get("error", "not launchable") + ". "
                                  + "; ".join(fresh.get("reasons", []))),
                        "status_code": int(ok.get("code", 409)), "gate": fresh}
        elif status in ("rejected", "launched"):
            # Terminal both ways even under the limit: a rejection must not be
            # re-launched, and re-launching an already-launched estimate would attach
            # two runs to one approval and double-count it in the variance report.
            return {"error": f"estimate is {status} and cannot launch",
                    "status_code": 409}
        for k in ("task_count", "sample_count"):
            if k not in params:
                try:
                    params[k] = int(json.loads(est.get("plan", "{}")).get(k))
                except Exception:
                    pass

    payload = {"trigger_source": "console", "params": params}
    r = lam.invoke(FunctionName=START_FN, InvocationType="RequestResponse",
                   Payload=json.dumps(payload).encode())
    try:
        out = json.loads(r["Payload"].read())
    except Exception:
        out = {"note": "started (no parseable response)"}
    if r.get("FunctionError"):
        return {"error": f"start-pipeline failed: {json.dumps(out, default=str)[:300]}"}

    if est is not None and estimates_tbl:
        # Stamp the run back onto the estimate. Without this link the variance report
        # has an estimate and an actual it cannot join, which is the whole point.
        try:
            estimates_tbl.update_item(
                Key={"id": est["id"]},
                UpdateExpression=("SET #s = :s, run_id = :r, sfn_execution_arn = :a, "
                                  "launched_at = :t"),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":s": "launched",
                    ":r": str(out.get("run_id") or out.get("runId") or ""),
                    ":a": str(out.get("execution_arn")
                              or out.get("executionArn") or ""),
                    ":t": datetime.now(timezone.utc).isoformat()})
        except Exception as e:
            # The run is already going; failing the response here would tell the caller
            # the launch failed when it did not. Log loudly instead.
            print(f"[finops] could not stamp estimate {est['id']}: {e}")
            out["warning"] = "run started but the estimate record was not updated"
        out["estimate_id"] = est["id"]
    return {"ok": True, "result": out}


# ── FinOps: estimates, the dual approval gate, actuals rollup ─────────────────
# Design premise for everything below: an estimate is a guess, but an *actual* must
# never be a guess. Every figure returned carries where it came from and whether it has
# settled, because a confidently-wrong cost number is worse than an admitted unknown —
# someone approves real spend on it.

def _cost_model():
    """Import the canonical estimator lazily.

    The zip may be built without pipeline/ (the console deploys independently of the
    pipeline package). Failing here must degrade the Cost tab, not 500 the whole
    dashboard, so the caller turns None into a visible 'estimator unavailable'.
    """
    try:
        import sys
        for cand in (os.path.join(_HERE, "pipeline", "contracts"),
                     os.path.join(_HERE, "contracts"), _HERE):
            if cand not in sys.path:
                sys.path.insert(0, cand)
        import cost_model  # noqa: PLC0415 — deliberately lazy
        return cost_model
    except Exception as e:
        print(f"[finops] cost_model unavailable: {e}")
        return None


def _rate_card():
    """Latest rate card from S3 as a RateCard, or None.

    Never fabricate rates on a read failure — an empty card makes every SKU 'unpriced',
    which is visibly not-an-estimate; a silently-substituted default would produce a
    plausible wrong number instead. Returning None rather than an empty RateCard keeps
    "we could not read the card" distinguishable from "the card is empty".
    """
    cm = _cost_model()
    if cm is None:
        return None
    try:
        o = s3.get_object(Bucket=data_bucket(),
                          Key="finops/rates/rate_card_latest.json")
        doc = json.loads(o["Body"].read())
        return cm.RateCard(doc.get("rates", doc))
    except Exception as e:
        print(f"[finops] no rate card: {e}")
        return None


def _f(v, default=0.0):
    """DynamoDB gives Decimal; JSON needs float. Decimal survives json.dumps only via
    the default= hook, which would stringify it and break arithmetic in the browser."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def project_to_date_usd(project=None):
    """Actual settled+provisional spend recorded for this project, all periods.

    Reads the ledger the finops agent writes. Audit/finding rows are skipped: they are
    the agent's own notes, and summing them would double-count the very variances they
    describe.
    """
    if not actuals_tbl:
        return 0.0, 0
    total, n = 0.0, 0
    try:
        resp = actuals_tbl.query(
            KeyConditionExpression=Key("project").eq(project or PROJECT))
        for it in resp.get("Items", []):
            sk = str(it.get("sk", ""))
            if "#audit#" in sk or "#finding#" in sk:
                continue
            total += _f(it.get("cost_usd"))
            n += 1
    except Exception as e:
        print(f"[finops] project_to_date failed: {e}")
    return round(total, 2), n


def gate_decision(worst_case_usd, project=None):
    """The dual threshold, delegated to cost_model.approval_decision.

    Deliberately not re-implemented here. The gate arithmetic is already tested in
    tests/test_cost_model.py, and a second copy in the console is a copy that can drift
    — the drifting one being the one that actually guards the launch button.
    """
    cm = _cost_model()
    ptd, _ = project_to_date_usd(project)
    if cm is None:
        # Fail CLOSED. With no estimator we cannot prove the run is under the limit, so
        # "we could not check" must land on the require-approval side.
        return {"approval_required": True, "project_to_date_usd": ptd,
                "gating_usd": _f(worst_case_usd), "status": "pending_approval",
                "reasons": ["cost model unavailable — cannot verify the spend limit, "
                            "so approval is required"]}
    return cm.approval_decision({"worst_case_usd": _f(worst_case_usd)},
                                project_to_date_usd=ptd,
                                single_run_limit_usd=APPROVAL_LIMIT_USD,
                                cumulative_limit_usd=CUMULATIVE_LIMIT_USD)


def create_estimate(body, username, now_iso):
    """Price a draft plan and record it, with its gate verdict already computed."""
    cm = _cost_model()
    if cm is None:
        return {"error": "estimator unavailable: cost_model.py not in this bundle",
                "status_code": 503}
    # Only keys estimate_run actually reads are forwarded, so a typo in the form does
    # not become a silently-ignored field that the operator believes was priced.
    INT_KEYS = ("sample_count", "task_count", "max_iterations", "n_stages")
    FLOAT_KEYS = ("endpoint_hours", "train_rows", "minutes_per_stage")
    STR_KEYS = ("training_instance", "inference_instance", "teacher_model",
                "harness_model")
    BOOL_KEYS = ("keep_reasoning", "teardown")
    plan = {}
    for k in INT_KEYS:
        if body.get(k) not in (None, ""):
            try:
                plan[k] = int(body[k])
            except Exception:
                return {"error": f"{k} must be an integer", "status_code": 400}
    for k in FLOAT_KEYS:
        if body.get(k) not in (None, ""):
            try:
                plan[k] = float(body[k])
            except Exception:
                return {"error": f"{k} must be a number", "status_code": 400}
    for k in STR_KEYS:
        if body.get(k):
            plan[k] = str(body[k])[:120]
    for k in BOOL_KEYS:
        if k in body:
            plan[k] = bool(body[k])
    if not (plan.get("sample_count") or plan.get("train_rows")):
        return {"error": "sample_count (or train_rows) is required — with neither, the "
                         "training line is $0 and the total is not an estimate",
                "status_code": 400}

    card = _rate_card()
    if card is None:
        # Refuse rather than return a $0-with-warnings total. A number on a screen gets
        # quoted; an explicit refusal does not.
        return {"error": "no rate card available — run pricing_refresh before "
                         "estimating; without rates every line would price at $0",
                "status_code": 503}
    try:
        est = cm.estimate_run(plan, card)
    except Exception as e:
        return {"error": f"estimate failed: {str(e)[:300]}", "status_code": 400}

    worst = _f(est.get("worst_case_usd"))
    gate = gate_decision(worst)
    eid = "est-" + secrets.token_hex(8)
    item = {
        "id": eid, "project": PROJECT, "created_at": now_iso,
        "requested_by": username or "anonymous",
        "plan": json.dumps(plan, default=str),
        # Numbers stored as strings, not Decimal: DynamoDB rejects float, and the whole
        # doc is round-tripped through json anyway.
        "estimate": json.dumps(est, default=str)[:380000],
        "total_usd": str(round(_f(est.get("total_usd")), 4)),
        "worst_case_usd": str(round(worst, 4)),
        "confidence": str(est.get("confidence", "unknown")),
        "n_unpriced": len(est.get("unpriced", []) or []),
        # draft = priced but not submitted. Submitting is a separate, explicit act, so a
        # user exploring numbers never accidentally files an approval request.
        "status": "draft",
        "gate": json.dumps(gate, default=str),
        # The rates live at estimate time, so a variance months later can be re-derived
        # against what was known then. Storing only "latest" makes old misses
        # unexplainable — the estimate looks wrong when the rate card simply moved.
        "rate_card_as_of": str(rate_card_health(plan).get("oldest_as_of") or ""),
    }
    if estimates_tbl:
        estimates_tbl.put_item(Item=item)
    return {"ok": True, "estimate_id": eid, "estimate": est, "gate": gate,
            "rate_card": rate_card_health(plan), "plan": plan}


def _get_estimate(estimate_id):
    if not (estimates_tbl and estimate_id):
        return None
    try:
        return estimates_tbl.get_item(Key={"id": str(estimate_id)}).get("Item")
    except Exception as e:
        print(f"[finops] get estimate failed: {e}")
        return None


def request_approval(body, username, now_iso):
    """Move a draft to pending_approval. Only a draft may be submitted."""
    eid = str(body.get("estimate_id", ""))
    item = _get_estimate(eid)
    if not item:
        return {"error": "unknown estimate_id", "status_code": 404}
    if item.get("status") != "draft":
        # Re-submitting a decided estimate would let a rejection be quietly retried
        # until someone approves it — the same estimate, a different approver, no record
        # that it was already refused.
        return {"error": f"estimate is {item.get('status')}, not draft",
                "status_code": 409}
    estimates_tbl.update_item(
        Key={"id": eid},
        UpdateExpression=("SET #s = :s, requested_by = :u, requested_at = :t, "
                          "justification = :j"),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": "pending_approval", ":u": username or "anonymous", ":t": now_iso,
            ":j": str(body.get("justification", ""))[:1000]})
    return {"ok": True, "estimate_id": eid, "status": "pending_approval"}


def decide_approval(body, user, now_iso):
    """Approve or reject. Enforces separation of duties server-side.

    Three checks, each of which has to be here rather than in the UI:
      1. approver-group membership — a hidden button is not an access control;
      2. approved_by != requested_by — self-approval defeats the whole gate;
      3. status must still be pending_approval — otherwise a decision can be replayed
         over an earlier one.
    """
    username = (user or {}).get("username", "")
    groups = (user or {}).get("groups", []) or []
    eid = str(body.get("estimate_id", ""))
    decision = str(body.get("decision", "")).lower()
    if decision not in ("approve", "reject"):
        return {"error": "decision must be 'approve' or 'reject'", "status_code": 400}
    item = _get_estimate(eid)
    if not item:
        return {"error": "unknown estimate_id", "status_code": 404}

    cm = _cost_model()
    if cm is None:
        # No estimator means the separation-of-duties rules cannot be evaluated. Deny;
        # an approval granted because the checker was missing is worse than no approval.
        return {"error": "cost model unavailable — cannot validate the approval",
                "status_code": 503}
    verdict = cm.check_approval(item, username, groups,
                               required_group=APPROVER_GROUP)
    if not verdict.get("allowed"):
        return {"error": verdict.get("error", "not allowed"),
                "status_code": int(verdict.get("code", 403))}
    reason = str(body.get("reason", ""))[:1000]
    if decision == "reject" and not reason:
        # A rejection with no reason cannot be acted on: the requester learns only that
        # someone said no, and re-submits the same estimate.
        return {"error": "a rejection needs a reason", "status_code": 400}

    new_status = "approved" if decision == "approve" else "rejected"
    estimates_tbl.update_item(
        Key={"id": eid},
        UpdateExpression=("SET #s = :s, approved_by = :u, decided_at = :t, "
                          "decision_reason = :r"),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": new_status, ":u": username, ":t": now_iso,
                                   ":r": reason})
    out = {"ok": True, "estimate_id": eid, "status": new_status,
           "approved_by": username}
    if decision == "approve" and body.get("launch"):
        launch = start_run({"estimate_id": eid, "note": f"approved by {username}"})
        # The nested status_code is dropped deliberately: the approval itself succeeded
        # and is recorded, so this response is a 200 carrying a launch error, not a
        # failed approval. Returning 4xx here would suggest the approval did not land.
        launch.pop("status_code", None)
        out["launch"] = launch
    return out


def cost_estimates(limit=50):
    """Estimates newest-first, plus the pending-approval queue."""
    items = []
    if estimates_tbl:
        try:
            r = estimates_tbl.query(
                IndexName="project-created_at-index",
                KeyConditionExpression=Key("project").eq(PROJECT),
                ScanIndexForward=False, Limit=int(limit))
            items = r.get("Items", [])
        except Exception as e:
            # A brand-new deployment has the table but an unbackfilled GSI; scanning is
            # correct here rather than showing an empty queue, which would read as
            # "nothing awaiting approval".
            print(f"[finops] GSI query failed, falling back to scan: {e}")
            try:
                items = estimates_tbl.scan(Limit=int(limit)).get("Items", [])
            except Exception as e2:
                print(f"[finops] estimates scan failed: {e2}")
    out = []
    for it in items:
        try:
            est = json.loads(it.get("estimate", "{}"))
        except Exception:
            est = {}
        try:
            gate = json.loads(it.get("gate", "{}"))
        except Exception:
            gate = {}
        out.append({
            "id": it.get("id"), "status": it.get("status"),
            "created_at": it.get("created_at"),
            "requested_by": it.get("requested_by"), "approved_by": it.get("approved_by"),
            "decided_at": it.get("decided_at"),
            "decision_reason": it.get("decision_reason"),
            "justification": it.get("justification"),
            "total_usd": _f(it.get("total_usd")),
            "worst_case_usd": _f(it.get("worst_case_usd")),
            "confidence": it.get("confidence"),
            "n_unpriced": int(_f(it.get("n_unpriced"))),
            "rate_card_as_of": it.get("rate_card_as_of"),
            "run_id": it.get("run_id"), "sfn_execution_arn": it.get("sfn_execution_arn"),
            "line_items": est.get("line_items", []),
            "subtotals": est.get("subtotals", {}),
            "unpriced": est.get("unpriced", []),
            "assumptions": est.get("assumptions", []),
            "gate": gate,
        })
    out.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
    return {"estimates": out,
            "pending": [e for e in out if e["status"] == "pending_approval"],
            "limits": {"single_usd": APPROVAL_LIMIT_USD,
                       "cumulative_usd": CUMULATIVE_LIMIT_USD,
                       "approver_group": APPROVER_GROUP}}


def cost_overview(period_days=30):
    """Project rollup: actual by service and category, with settlement state.

    Two numbers no dashboard should merge: settled and provisional. Cost Explorer lags
    ~24 h, so the trailing days will move. Showing one blended total invites quoting a
    figure that has not landed.
    """
    rows, audit = [], []
    if actuals_tbl:
        try:
            r = actuals_tbl.query(
                KeyConditionExpression=Key("project").eq(PROJECT),
                ScanIndexForward=False, Limit=2000)
            for it in r.get("Items", []):
                sk = str(it.get("sk", ""))
                if "#audit#" in sk or "#finding#" in sk:
                    audit.append({"sk": sk, "task": it.get("task"),
                                  "tool": it.get("tool"), "status": it.get("status")})
                    continue
                parts = sk.split("#")
                rows.append({
                    "period": parts[0] if parts else "",
                    "run_id": parts[1] if len(parts) > 1 else "",
                    "category": parts[2] if len(parts) > 2 else it.get("category", ""),
                    "service": it.get("service", ""),
                    "resource_id": it.get("resource_id", ""),
                    "cost_usd": _f(it.get("cost_usd")),
                    "quantity": _f(it.get("quantity")),
                    "unit": it.get("unit", ""),
                    "settlement": it.get("settlement", "provisional"),
                    "rate_source": it.get("rate_source", ""),
                })
        except Exception as e:
            print(f"[finops] actuals query failed: {e}")

    by_cat, by_svc, by_run, by_period = {}, {}, {}, {}
    settled = provisional = 0.0
    for r in rows:
        c = r["cost_usd"]
        by_cat[r["category"] or "unknown"] = round(
            by_cat.get(r["category"] or "unknown", 0.0) + c, 4)
        by_svc[r["service"] or "unknown"] = round(
            by_svc.get(r["service"] or "unknown", 0.0) + c, 4)
        if r["run_id"]:
            by_run[r["run_id"]] = round(by_run.get(r["run_id"], 0.0) + c, 4)
        by_period[r["period"]] = round(by_period.get(r["period"], 0.0) + c, 4)
        if r["settlement"] == "settled":
            settled += c
        else:
            provisional += c

    budgets = []
    try:
        b = boto3.client("budgets", region_name=REGION)
        for bd in b.describe_budgets(AccountId=ACCOUNT_ID, MaxResults=20).get("Budgets", []):
            budgets.append({
                "name": bd.get("BudgetName"),
                "limit_usd": _f((bd.get("BudgetLimit") or {}).get("Amount")),
                "actual_usd": _f(((bd.get("CalculatedSpend") or {}).get("ActualSpend")
                                  or {}).get("Amount")),
                "time_unit": bd.get("TimeUnit"),
            })
    except Exception as e:
        # An account guardrail we cannot read is worth saying so about, not hiding.
        print(f"[finops] budgets unavailable: {e}")

    return {
        "project": PROJECT,
        "total_usd": round(settled + provisional, 2),
        "settled_usd": round(settled, 2),
        "provisional_usd": round(provisional, 2),
        "n_line_items": len(rows),
        "by_category": by_cat, "by_service": by_svc,
        "by_run": dict(sorted(by_run.items(), key=lambda kv: -kv[1])[:25]),
        "by_period": dict(sorted(by_period.items(), reverse=True)[:int(period_days)]),
        "line_items": rows[:400],
        "audit_rows": audit[:50],
        "budgets": budgets,
        "limits": {"single_usd": APPROVAL_LIMIT_USD,
                   "cumulative_usd": CUMULATIVE_LIMIT_USD},
        "note": ("Attribution is by explicit resource match (training-job/llmops-*, "
                 "manifest endpoints, spans by session id) — never by service, because "
                 "this account also carries unrelated SageMaker spend. Cost Explorer "
                 "lags ~24h, so provisional figures will still move."),
    }


def rate_card_health(plan=None):
    """What the rate card knows and what it admits it does not.

    This panel exists because the Price List API cannot price Fable 5 or Opus 5 on this
    account (verified 2026-07-31: every `provider=Anthropic` entry for us-east-1 is
    Claude 3 or older) — the models the harness fleet itself runs on. Without the panel,
    a stale feed prices the agent fleet at $0 and the estimate is quietly, plausibly
    wrong.

    Health is measured against the SKUs a real plan NEEDS, not against the card's own
    contents — a card with 40 irrelevant rates and no teacher price is not healthy, and
    only `required_skus_for` can tell the difference.
    """
    cm = _cost_model()
    card = _rate_card()
    if cm is None:
        return {"present": False, "warning": "cost model unavailable in this bundle"}
    if card is None:
        return {"present": False, "healthy": False,
                "warning": "no rate card in S3 — run pricing_refresh; until then every "
                           "SKU is unpriced and estimates are not usable"}
    out = cm.rate_card_health(card, cm.required_skus_for(plan or {}))
    out["present"] = True
    return out


def cost_variance(estimates=None, overview=None):
    """Estimate vs actual per run, naming the driving category rather than one aggregate %.

    A single "we were 40% off" tells nobody what to fix; the next estimate is only better
    if the miss is attributed to a line — so this delegates to cost_model.reconcile,
    which is where that attribution lives and is tested.

    Both inputs are injectable so the caller can reuse reads it already made; recomputing
    them would double the DynamoDB queries behind one page render.
    """
    cm = _cost_model()
    ests = (estimates if estimates is not None
            else cost_estimates(limit=100)["estimates"])
    ov = overview if overview is not None else cost_overview()

    # Per-run, per-category actuals, assembled from the same rows the rollup used.
    by_run_cat = {}
    settle = {}
    for r in ov.get("line_items", []):
        rid = r.get("run_id")
        if not rid:
            continue
        cat = by_run_cat.setdefault(rid, {})
        cat[r.get("category") or "unknown"] = round(
            cat.get(r.get("category") or "unknown", 0.0) + _f(r.get("cost_usd")), 6)
        # A run is only settled if EVERY row for it is: one provisional row means the
        # total can still move, and calling that settled is the error this guards.
        if r.get("settlement") != "settled":
            settle[rid] = "provisional"
        else:
            settle.setdefault(rid, "settled")

    out = []
    for e in ests:
        rid = e.get("run_id")
        if not rid or rid not in by_run_cat:
            continue
        actual = {"subtotals": by_run_cat[rid],
                  "total_usd": round(sum(by_run_cat[rid].values()), 6),
                  "settlement": settle.get(rid, "provisional")}
        est_doc = {"subtotals": e.get("subtotals") or {},
                   "total_usd": e.get("total_usd", 0.0)}
        rec = (cm.reconcile(est_doc, actual) if cm else
               {"estimate_usd": e.get("total_usd"), "actual_usd": actual["total_usd"],
                "verdict": "cost model unavailable — cannot attribute the variance"})
        rec.update({"run_id": rid, "estimate_id": e.get("id"),
                    "confidence": e.get("confidence")})
        out.append(rec)

    matched = {o["run_id"] for o in out}
    unestimated = [r for r in by_run_cat if r not in matched]
    return {"variance": out,
            # Stated explicitly: a variance report silent about unestimated spend
            # implies full coverage it does not have. Runs launched without an estimate
            # are legal — that is how every run worked before this feature.
            "unestimated_runs": sorted(unestimated)[:50],
            "n_unestimated": len(unestimated)}


def finops_run(body):
    """Trigger reconcile / pricing_refresh / report on demand (the schedule is daily)."""
    task = str(body.get("task", "reconcile"))
    if task not in ("reconcile", "pricing_refresh", "report"):
        return {"error": f"unknown task {task}", "status_code": 400}
    payload = {"task": task, "project": PROJECT, "sync": False}
    if body.get("period"):
        payload["period"] = str(body["period"])
    try:
        r = lam.invoke(FunctionName=FINOPS_FN, InvocationType="Event",
                       Payload=json.dumps(payload).encode())
        # Named invoke_status, NOT status_code: _resp_result treats status_code as the
        # HTTP status to return, and Lambda's async 202 would silently become this
        # route's response code.
        return {"ok": True, "task": task, "invoke_status": r.get("StatusCode"),
                "note": "async — watch the rollup; Cost Explorer lags ~24 h"}
    except Exception as e:
        return {"error": f"could not invoke {FINOPS_FN}: {str(e)[:300]}",
                "status_code": 502}


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


def _resp_result(result):
    """Let a handler choose its own status code without knowing about HTTP.

    The cost handlers return {"error": ..., "status_code": 400|403|409}. Flattening those
    to 200-with-an-error-body would make a denied approval indistinguishable from a
    granted one to anything but a human reading the JSON — and audit tooling reads
    status codes.

    Only an EXPLICIT status_code is honoured; anything else stays 200. Inferring 400
    from the presence of an "error" key would silently re-code the seven pre-existing
    POST routes, whose frontend handlers all read `j.error` off a 200.
    """
    if isinstance(result, dict) and result.get("status_code"):
        code = int(result.pop("status_code"))
        return _resp(code, result)
    return _resp(200, result)


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


def _authed_user(headers):
    """Return {"username", "groups"} for a valid token, else None.

    GetUser validates the token server-side (signature, expiry, revocation) and returns
    the username — but NOT group membership, and a bearer *access* token carries no
    cognito:groups claim either. So the approver check needs a second call,
    AdminListGroupsForUser. Getting this wrong would leave the separation-of-duties gate
    with nothing to check: it would read as enforced while approving everything.

    A groups lookup that fails yields an EMPTY group list rather than an exception. The
    caller then denies, because "we could not prove you are an approver" must land on
    the deny side; a throttled Cognito call must not become an approval.
    """
    h = {k.lower(): v for k, v in (headers or {}).items()}
    auth = h.get("authorization", "")
    if not (auth.startswith("Bearer ") and COGNITO_POOL_ID):
        return None
    try:
        who = cognito.get_user(AccessToken=auth[7:].strip())
    except Exception:
        return None
    username = who.get("Username", "")
    groups = []
    try:
        g = cognito.admin_list_groups_for_user(UserPoolId=COGNITO_POOL_ID,
                                               Username=username, Limit=60)
        groups = [x.get("GroupName", "") for x in g.get("Groups", [])]
    except Exception as e:
        print(f"[auth] group lookup failed for {username}: {e}")
    return {"username": username, "groups": groups}


def _authed(headers):
    """Thin bool wrapper so the pre-existing POST routes keep their exact contract."""
    return _authed_user(headers) is not None


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

    if method == "GET" and path in ARCH_SVGS:
        svg = ARCH_SVGS[path]
        if svg is None:
            return _resp(404, {"error": f"{path} missing from the deployed bundle"})
        return _resp(200, svg, "image/svg+xml; charset=utf-8")

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
        if method == "GET" and path == "/api/cost-overview":
            out = cost_overview(qs.get("days", 30))
            out["rate_card"] = rate_card_health()
            return _resp(200, out)
        if method == "GET" and path == "/api/cost-estimates":
            out = cost_estimates(qs.get("limit", 50))
            # Reuses the reads above rather than re-querying both tables — one page
            # render should not cost four DynamoDB queries where two suffice.
            out["variance"] = cost_variance(estimates=out["estimates"],
                                            overview=cost_overview())
            return _resp(200, out)

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
            # One auth call for every POST, resolved to a user rather than a bool: the
            # cost routes need the username (self-approval check) and the groups
            # (approver check), and re-calling Cognito per route would double the
            # latency of the approval click for no benefit.
            user = _authed_user(headers)
            if user is None:
                return _resp(401, {"error": "unauthorized"})
            now = datetime.now(timezone.utc).isoformat()
            if path == "/api/start-run":
                return _resp_result(start_run(body))
            if path == "/api/cost-estimate":
                return _resp_result(create_estimate(body, user["username"], now))
            if path == "/api/cost-approval-request":
                return _resp_result(request_approval(body, user["username"], now))
            if path == "/api/cost-approval":
                return _resp_result(decide_approval(body, user, now))
            if path == "/api/finops-run":
                return _resp_result(finops_run(body))
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
