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

And (--retention) a retention policy on every log group this system fills. Measured
2026-08-12 over seven days: 1236 MB ingested into llmops log groups, and 1225 MB of it
landed in groups that NEVER expire -- while the delivery groups that do carry a 30-day
policy received 0 bytes. See retention_targets() for the two sources and why one of them
has to be listed from the ACCOUNT: the groups with the traffic are named after ids this
repo never sees, so building their names from HARNESSES creates empty groups beside them.

Usage:
  python deploy/06_observability.py --region us-east-1 --dry-run
  python deploy/06_observability.py --region us-east-1              # deliveries only
  python deploy/06_observability.py --region us-east-1 --evals      # + online eval configs
  python deploy/06_observability.py --region us-east-1 --alarms     # + Lambda alarms
  python deploy/06_observability.py --region us-east-1 --retention  # + log retention
"""
import argparse
import ast
import json
import pathlib
import re
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
    for rt in list_runtimes(ctl):
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


#: The admin console's API Lambda is created by deploy/console/deploy.sh, not by
#: 07_lambdas.py, and reading its name from that script is the whole point of this
#: constant: deriving the census from ONE deploy script produced exactly the outcome
#: derivation was supposed to prevent. Measured 2026-08-12: the account runs EIGHT
#: llmops functions, the seven in 07_lambdas.py were the entire alarm list, and the
#: eighth -- llmops-admin, 17,007 invocations in three days, the surface every plan
#: signature and every human verdict goes through -- had no alarm of any kind. The
#: derivation was never wrong about what 07_lambdas.py deploys; the CLAIM was, because
#: "what one deploy script creates" is not "what this system runs".
CONSOLE_DEPLOY = "console/deploy.sh"


def console_function() -> str:
    """The console Lambda's name, read from the variable deploy.sh creates it with.

    A shell variable rather than an ast walk because that is what the file has, and the
    same rule applies as for LAMBDAS: a missing assignment is a hard stop, not an empty
    census. Silently returning nothing here would restore the exact defect this fixes.
    """
    m = re.search(r"^FN=([A-Za-z0-9_.-]+)\s*$", (REPO / CONSOLE_DEPLOY).read_text(),
                  re.M)
    if not m:
        raise SystemExit(f"{CONSOLE_DEPLOY} has no FN= assignment — alarm list unknown")
    return m.group(1)


def deployed_functions() -> list:
    """Every llmops Lambda the deploy scripts create, derived from each of them.

    Derived, not listed: a hand-kept copy of this list is how the eighth Lambda ends
    up as the one nobody alarms on. 07_lambdas.py is parsed with ast rather than
    imported because the module name starts with a digit (and importing it would need
    boto3 credentials); the console's is read from its shell variable. See
    CONSOLE_DEPLOY for why one script was not enough -- the eighth Lambda was real.
    """
    tree = ast.parse((REPO / "07_lambdas.py").read_text())
    fns = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", "") == "LAMBDAS" for t in node.targets)):
            fns = [v.value for k, spec in zip(node.value.keys, node.value.values)
                   for kk, v in zip(spec.keys, spec.values)
                   if getattr(kk, "value", "") == "fn"]
            break
    if fns is None:
        raise SystemExit("07_lambdas.py has no LAMBDAS assignment — alarm list unknown")
    console = console_function()
    return fns + ([console] if console not in fns else [])


#: How long every log group this system fills keeps data. Not a new number:
#: setup_observability.py has defaulted to 30 days since the deliveries were first wired, so
#: --retention makes the SAME convention reach the groups nobody was applying it to. The
#: record that has to outlive 30 days is the audit trail in DynamoDB (stage events + run
#: rows) and the artifacts in S3, neither of which is a log group.
RETENTION_DAYS = 30

#: Where this system's AgentCore log groups live, and the substring that says a group under
#: it is ours. Discovered rather than derived, and that is a measurement, not a preference:
#: the group that actually holds an agent's application logs and spans is
#: `/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT`, whose id nothing in this repo
#: knows, and the delivery groups' real names carry the harness id too
#: (`llmops_data_prep-KuSKXUaxyP`), which HARNESSES does not have. Building these names from
#: HARNESSES creates five EMPTY groups beside the ones with the traffic -- tried, measured,
#: discarded.
AGENTCORE_LOGS = "/aws/bedrock-agentcore/"
OURS = "llmops"


def list_runtimes(ctl) -> list:
    """Every AgentCore runtime, paginated. The unpaginated call is a silent undercount.

    Measured 2026-08-12: `list_agent_runtimes()` returned 10 of 19 runtimes with a
    nextToken nobody followed -- and the nine it left out included harness_llmops_data_prep,
    the largest log producer in the account. Its caller in online_eval() reports
    "no runtime <name>" and skips creating that harness's evaluation config, so a runtime
    that exists reads as one that does not, purely by page position. Same defect as the
    unpaginated list_functions() that reported 3 of 8 Lambdas.
    """
    out, token = [], None
    while True:
        resp = ctl.list_agent_runtimes(**({"nextToken": token} if token else {}))
        out += resp.get("agentRuntimes", [])
        token = resp.get("nextToken")
        if not token:
            return out


def retention_targets(logs) -> list:
    """Every log group this system fills. Two sources, because two things create them.

    * `/aws/lambda/<fn>` for every function ANY deploy script creates (see CONSOLE_DEPLOY) --
      REPO-derived, because the repo is what creates those functions. Lambda creates the
      group itself on first invoke, with no retention, so nothing here had ever set one.
    * every group under AGENTCORE_LOGS whose name contains OURS -- ACCOUNT-derived, because
      AgentCore creates these and names them after ids the repo never sees. This is where
      the volume is: measured over seven days to 2026-08-12, 1236 MB ingested into this
      system's log groups, 1225 MB of it into never-expiring runtime DEFAULT groups, while
      the delivery groups that DO carry a 30-day policy received 0 bytes. The retention
      control existed and was pointed at the surface with no traffic.

    Known bound, stated rather than hidden: a runtime created AFTER this runs has an
    unbounded group until the next run. A group that does not exist holds no data, so the
    exposure starts at the first invoke, not at create.
    """
    targets = [f"/aws/lambda/{fn}" for fn in deployed_functions()]
    for page in logs.get_paginator("describe_log_groups").paginate(
            logGroupNamePrefix=AGENTCORE_LOGS):
        targets += [g["logGroupName"] for g in page["logGroups"]
                    if OURS in g["logGroupName"]]
    return targets


def retention(logs, dry):
    """Put RETENTION_DAYS on every group retention_targets() names.

    `create_log_group` first, ignoring "already exists": a Lambda's group does not exist
    until its first invoke, and a policy cannot be put on a group that is not there -- so
    without the create, a freshly deployed function would keep its logs forever until
    someone re-ran this after the first invocation, which is precisely the kind of ordering
    nobody remembers. Deleting nothing today is checked, not assumed: the oldest stream in
    this account is 2026-07-29, 14 days inside a 30-day window.
    """
    out = []
    for group in retention_targets(logs):
        if dry:
            out.append({"group": group, "would_set_days": RETENTION_DAYS})
            continue
        try:
            logs.create_log_group(logGroupName=group)
            created = True
        except logs.exceptions.ResourceAlreadyExistsException:
            created = False
        logs.put_retention_policy(logGroupName=group, retentionInDays=RETENTION_DAYS)
        out.append({"group": group, "days": RETENTION_DAYS, "created": created})
    return out


def alarms(cw, topic_arn, dry):
    """Three families, each detecting something the other two cannot.

    `<fn>-errors` (every deployed Lambda, from BOTH deploy scripts -- see
    CONSOLE_DEPLOY): the primary detector. Sum(Errors) >= 1 over five minutes.
    `notBreaching` on missing data because a Lambda nobody invoked has no error to
    report. The console Lambda is in this family only: nothing schedules it, so silence
    is normal, and nothing invokes it asynchronously, so it cannot drop an event.

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
    ap.add_argument("--retention", action="store_true",
                    help=f"also cap every log group this system fills at {RETENTION_DAYS} days")
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
    if args.retention:
        results["retention"] = retention(
            boto3.client("logs", region_name=args.region), args.dry_run)
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
