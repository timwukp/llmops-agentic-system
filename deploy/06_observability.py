#!/usr/bin/env python3
"""06_observability.py — wire full AgentCore observability for every harness.

Wraps the agentcore-harness-builder skill's setup_observability.py per harness:
APPLICATION_LOGS -> CloudWatch Logs and TRACES -> X-Ray deliveries, log-group
retention, and the log-delivery resource policy. The OTEL_TRACES_SAMPLER=always_on
env var itself is injected at harness create/update time by 05_harnesses.py —
both halves are required or the ops console's Evaluations/Optimizations tabs
sit empty forever.

Also (--evals) attaches the three Builtin online evaluation configs the ops
console reads (Correctness, GoalSuccessRate, ToolSelectionAccuracy) to each
harness's traces.

And (--alarms) the CloudWatch alarms on the control-plane Lambdas. Until they
existed, every one of this system's failures was found by a human reading logs
hours later: 19 async invocations were DROPPED between 2026-07-29 and 2026-08-12
(driver 11, resume 8) and each one is a pipeline stage that stopped and told
nobody. See alarms() for what each family detects and why.

Usage:
  python deploy/06_observability.py --region us-east-1 --dry-run
  python deploy/06_observability.py --region us-east-1              # deliveries only
  python deploy/06_observability.py --region us-east-1 --evals      # + online eval configs
  python deploy/06_observability.py --region us-east-1 --alarms     # + Lambda alarms
"""
import argparse
import ast
import json
import pathlib
import secrets
import subprocess
import sys

import boto3

REPO = pathlib.Path(__file__).resolve().parent
HARNESSES = ["llmops_data_prep", "llmops_finetune", "llmops_eval",
             "llmops_deploy", "llmops_monitor"]
BUILTIN_EVALUATORS = ["Builtin.Correctness", "Builtin.GoalSuccessRate",
                      "Builtin.ToolSelectionAccuracy"]

#: Where every alarm notifies. Same topic the pipeline's own ESCALATED_TO_HUMAN
#: path uses: an operator watching one place is the point.
TOPIC_PARAM = "/llmops/storage/escalations_topic_arn"

#: The scheduled Lambdas whose SILENCE is itself a failure, and how long a silence
#: has to last to mean it. Keys are the schedule names in 08_triggers.py; only the
#: schedules that script enables BY DEFAULT belong here, which is why
#: llmops-start-pipeline is absent: its `llmops-nightly` schedule ships DISABLED
#: (--enable-schedule opts in), so a silence alarm on it would sit in ALARM forever
#: and teach the operator to ignore the whole set. A test derives this from
#: 08_triggers.py so flipping a default reds instead of rotting.
SILENCE_ALARMS = {
    # 15-minute sweep: an hour of silence is four missed sweeps, and the whole
    # point of this function is that it is the thing that notices.
    "llmops-resurrector-15min": {"fn": "llmops-resurrector", "period": 3600},
    # Daily: two periods, so one late run is not a page.
    "llmops-monitor-sweep-daily": {"fn": "llmops-monitor-sweep", "period": 86400,
                                   "periods": 2},
    "llmops-finops-daily": {"fn": "llmops-finops-reconcile", "period": 86400,
                            "periods": 2},
}

#: The two functions Lambda invokes ASYNCHRONOUSLY (the driver's turn handoff and
#: its resurrection; EventBridge's job-state delivery to resume). Only an async
#: invoke can be dropped, so only these two can raise AsyncEventsDropped.
ASYNC_DELIVERED = ["llmops-harness-driver", "llmops-resume-pipeline"]


def deliveries(region, harness, dry):
    """Delegate to the skill's battle-tested setup_observability.py (idempotent)."""
    cmd = [sys.executable, str(REPO / "setup_observability.py"),
           "--region", region, "--harness-id", harness,
           "--log-group", f"/aws/bedrock-agentcore/{harness}"]
    if dry:
        return {"harness": harness, "would_run": " ".join(cmd[1:])}
    out = subprocess.run(cmd, capture_output=True, text=True)
    return {"harness": harness, "rc": out.returncode,
            "tail": (out.stdout or out.stderr).strip().splitlines()[-1] if (out.stdout or out.stderr) else ""}


def online_eval(ctl, region, harness, role_arn, dry):
    """One online evaluation config per harness over the three Builtin evaluators.

    Real API shape (live-introspected 2026-07-29 — differs from console labels):
    onlineEvaluationConfigName + rule.samplingConfig.samplingPercentage +
    dataSourceConfig.cloudWatchLogs{logGroupNames, serviceNames} +
    evaluators[{evaluatorId}] + evaluationExecutionRoleArn + enableOnCreate.
    Data source = the runtime's DEFAULT OTel log group; serviceName = runtime name.
    """
    # config names only allow [a-zA-Z0-9_]
    name = f"{harness.replace('-', '_')}_online_eval"[:64]
    runtime_name = f"harness_{harness.rsplit('-', 1)[0]}"
    if dry:
        return {"harness": harness, "would_create": name}
    existing = [c for c in ctl.list_online_evaluation_configs()
                .get("onlineEvaluationConfigSummaries", [])
                if c.get("onlineEvaluationConfigName") == name]
    if existing:
        return {"harness": harness, "eval_config": name, "action": "exists"}
    # resolve the runtime id for the DEFAULT log group
    runtime_id = None
    for rt in ctl.list_agent_runtimes().get("agentRuntimes", []):
        if rt.get("agentRuntimeName") == runtime_name:
            runtime_id = rt.get("agentRuntimeId")
            break
    if not runtime_id:
        return {"harness": harness, "error": f"no runtime {runtime_name}"}
    log_group = f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"
    resp = ctl.create_online_evaluation_config(
        clientToken=secrets.token_hex(20),
        onlineEvaluationConfigName=name,
        rule={"samplingConfig": {"samplingPercentage": 100.0}},
        dataSourceConfig={"cloudWatchLogs": {
            # serviceName must match the span resource's service.name, which is
            # "<runtime-name>.DEFAULT" (endpoint-qualified) — the bare runtime
            # name silently matches ZERO spans and the evaluator scores nothing
            # forever ("awaiting traffic"). Live-diagnosed vs a working config.
            "logGroupNames": [log_group], "serviceNames": [f"{runtime_name}.DEFAULT"]}},
        evaluators=[{"evaluatorId": e} for e in BUILTIN_EVALUATORS],
        evaluationExecutionRoleArn=role_arn,
        enableOnCreate=True,
        tags={"project": "llmops-agentic-system"},
    )
    return {"harness": harness, "eval_config": name, "action": "created",
            "arn_field": "created"}


def deployed_functions() -> list:
    """The function names 07_lambdas.py deploys, read from its LAMBDAS literal.

    Derived, not listed: a hand-kept copy of this list is how the eighth Lambda ends
    up as the one nobody alarms on. Parsed with ast rather than imported because the
    module name starts with a digit (and importing it would need boto3 credentials).
    """
    tree = ast.parse((REPO / "07_lambdas.py").read_text())
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", "") == "LAMBDAS" for t in node.targets)):
            return [v.value for k, spec in zip(node.value.keys, node.value.values)
                    for kk, v in zip(spec.keys, spec.values)
                    if getattr(kk, "value", "") == "fn"]
    raise SystemExit("07_lambdas.py has no LAMBDAS assignment — alarm list unknown")


def alarms(cw, topic_arn, dry):
    """Three families, each detecting something the other two cannot.

    `<fn>-errors` (every deployed Lambda): the primary detector. Sum(Errors) >= 1 over
    five minutes. `notBreaching` on missing data because a Lambda nobody invoked has no
    error to report.

    `<fn>-silent` (the scheduled Lambdas only): Sum(Invocations) < 1. TreatMissingData
    MUST be `breaching` here -- an uninvoked function publishes NO datapoint rather than
    a zero, so with the usual `notBreaching` this alarm would sit in INSUFFICIENT_DATA
    forever and detect exactly nothing. This is the family that would have caught a
    schedule someone disabled by hand, which no error metric can see.

    `<fn>-async-dropped` (the two async-delivered Lambdas): Sum(AsyncEventsDropped) >= 1
    -- Lambda gave up on an event. Measured, not assumed: all 19 drops between
    2026-07-29 and 2026-08-12 landed on a day that also had function Errors (driver 11
    drops / 40 errors, resume 8 drops / 24 errors -- resume's ratio is exactly 3, the
    default 1 attempt + 2 retries), so the errors alarm above fires FIRST every time and
    this family is never the earlier warning. It is kept because it means something the
    errors alarm does not: an error that retried successfully is self-healed, while a
    drop is WORK THAT IS GONE -- a run stalled with its token parked, waiting for the
    resurrector. It also covers the drop with no error at all (event age-out, throttle
    exhaustion), which has not happened here yet.

    No Step Functions ExecutionsFailed alarm: a run that fails its quality gate is a
    designed outcome of this pipeline, not an incident, and an alarm that fires on
    correct behaviour is one an operator learns to close.
    """
    common = {"ActionsEnabled": True, "AlarmActions": [topic_arn] if topic_arn else [],
              "Namespace": "AWS/Lambda", "Statistic": "Sum"}
    plans = []
    for fn in deployed_functions():
        plans.append({**common, "AlarmName": f"{fn}-errors",
                      "AlarmDescription": f"{fn} raised. Every dropped async event this "
                                          "system has had was preceded by one of these.",
                      "MetricName": "Errors", "Period": 300, "EvaluationPeriods": 1,
                      "Threshold": 1.0, "TreatMissingData": "notBreaching",
                      "ComparisonOperator": "GreaterThanOrEqualToThreshold",
                      "Dimensions": [{"Name": "FunctionName", "Value": fn}]})
    for schedule, spec in sorted(SILENCE_ALARMS.items()):
        fn, periods = spec["fn"], spec.get("periods", 1)
        plans.append({**common, "AlarmName": f"{fn}-silent",
                      "AlarmDescription": f"{fn} has not run for "
                                          f"{spec['period'] * periods // 60} minutes; "
                                          f"schedule {schedule} should be firing it.",
                      "MetricName": "Invocations", "Period": spec["period"],
                      "EvaluationPeriods": periods, "Threshold": 1.0,
                      # breaching, or this alarm can never leave INSUFFICIENT_DATA
                      "TreatMissingData": "breaching",
                      "ComparisonOperator": "LessThanThreshold",
                      "Dimensions": [{"Name": "FunctionName", "Value": fn}]})
    for fn in ASYNC_DELIVERED:
        plans.append({**common, "AlarmName": f"{fn}-async-dropped",
                      "AlarmDescription": f"Lambda dropped an async event for {fn}: a "
                                          "stage stopped and told nobody (2026-08-08: "
                                          "one drop, nine hours dead at 4/55 tasks).",
                      "MetricName": "AsyncEventsDropped", "Period": 300,
                      "EvaluationPeriods": 1, "Threshold": 1.0,
                      "TreatMissingData": "notBreaching",
                      "ComparisonOperator": "GreaterThanOrEqualToThreshold",
                      "Dimensions": [{"Name": "FunctionName", "Value": fn}]})

    out = []
    for p in plans:
        if dry:
            out.append({"alarm": p["AlarmName"], "would_create": p["MetricName"],
                        "missing_data": p["TreatMissingData"]})
            continue
        cw.put_metric_alarm(**p)   # upsert: safe to re-run
        out.append({"alarm": p["AlarmName"], "action": "put"})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", required=True)
    ap.add_argument("--harness", action="append")
    ap.add_argument("--evals", action="store_true", help="also attach online eval configs")
    ap.add_argument("--alarms", action="store_true",
                    help="also create the Lambda CloudWatch alarms ($0.10/alarm/month)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    targets = args.harness or HARNESSES

    results = {"deliveries": [deliveries(args.region, h, args.dry_run) for h in targets]}
    if args.evals:
        ctl = boto3.client("bedrock-agentcore-control", region_name=args.region)
        role_arn = None
        if not args.dry_run:
            ssm = boto3.client("ssm", region_name=args.region)
            role_arn = ssm.get_parameter(Name="/llmops/iam/eval_execution_arn")["Parameter"]["Value"]
        results["online_evals"] = [online_eval(ctl, args.region, h, role_arn, args.dry_run)
                                   for h in targets]
    if args.alarms:
        topic_arn = None
        if not args.dry_run:
            ssm = boto3.client("ssm", region_name=args.region)
            topic_arn = ssm.get_parameter(Name=TOPIC_PARAM)["Parameter"]["Value"]
        results["alarms"] = alarms(boto3.client("cloudwatch", region_name=args.region),
                                   topic_arn, args.dry_run)

    print(json.dumps({**results, "dry_run": args.dry_run}, indent=2))


if __name__ == "__main__":
    sys.exit(main())
