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

Usage:
  python deploy/06_observability.py --region us-east-1 --dry-run
  python deploy/06_observability.py --region us-east-1              # deliveries only
  python deploy/06_observability.py --region us-east-1 --evals      # + online eval configs
"""
import argparse
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", required=True)
    ap.add_argument("--harness", action="append")
    ap.add_argument("--evals", action="store_true", help="also attach online eval configs")
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

    print(json.dumps({**results, "dry_run": args.dry_run}, indent=2))


if __name__ == "__main__":
    sys.exit(main())
