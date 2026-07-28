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


def online_eval(ctl, harness, dry):
    """One online evaluation config per harness over the three Builtin evaluators."""
    name = f"{harness}-online-eval"
    if not dry:
        existing = [c for c in ctl.list_online_evaluation_configs().get("onlineEvaluationConfigs", [])
                    if c.get("name") == name]
        if existing:
            return {"harness": harness, "eval_config": name, "action": "exists"}
    body = {
        "name": name,
        "evaluators": [{"builtinEvaluator": {"name": e}} for e in BUILTIN_EVALUATORS],
        "rule": {"samplingRule": {"rate": 1.0}},
        "clientToken": secrets.token_hex(20),
    }
    if dry:
        return {"harness": harness, "would_create": name}
    # The config binds to the harness runtime's trace source; introspect live shape
    # (preflight.py --show-shape CreateOnlineEvaluationConfig) if this call drifts.
    resp = ctl.create_online_evaluation_config(
        **body,
        target={"harnessTarget": {"harnessId": harness}},
    )
    return {"harness": harness, "eval_config": name, "action": "created",
            "id": resp.get("onlineEvaluationConfig", {}).get("id", "?")}


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
        results["online_evals"] = [online_eval(ctl, h, args.dry_run) for h in targets]

    print(json.dumps({**results, "dry_run": args.dry_run}, indent=2))


if __name__ == "__main__":
    sys.exit(main())
