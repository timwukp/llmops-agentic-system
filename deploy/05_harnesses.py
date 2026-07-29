#!/usr/bin/env python3
"""05_harnesses.py — create/update the five worker harnesses from agents/*/harness.json.

Thin orchestrator over the agentcore-harness-builder skill's create_harness.py /
update_harness.py conventions: reads each config, strips `_`-prefixed comment keys,
injects the execution role from SSM, creates the harness (or updates it if it
already exists), waits READY, and publishes ids to SSM /llmops/harness/<name>.

Also sets the observability env var the ops console requires on EVERY harness:
OTEL_TRACES_SAMPLER=always_on (without it, evaluations/insights sit at zero).

Usage:
  python deploy/05_harnesses.py --region us-east-1 --dry-run
  python deploy/05_harnesses.py --region us-east-1                       # all five
  python deploy/05_harnesses.py --region us-east-1 --agent data-prep     # one
  python deploy/05_harnesses.py --region us-east-1 --agent data-prep --prod  # harness.prod.json
"""
import argparse
import json
import pathlib
import secrets
import sys
import time

import boto3

REPO = pathlib.Path(__file__).resolve().parent.parent
AGENTS = ["data-prep", "finetune", "eval", "deploy", "monitor", "orchestrator"]


def strip_comments(obj):
    if isinstance(obj, dict):
        return {k: strip_comments(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [strip_comments(v) for v in obj]
    return obj


def load_config(agent, prod):
    fname = "harness.prod.json" if prod else "harness.json"
    path = REPO / "agents" / agent / fname
    if not path.exists():
        raise FileNotFoundError(f"{path} (run with/without --prod?)")
    return strip_comments(json.loads(path.read_text()))


def ensure_env(cfg):
    env = cfg.setdefault("environmentVariables", {})
    env.setdefault("OTEL_TRACES_SAMPLER", "always_on")
    return cfg


def existing_harness(ctl, name):
    """harnessId is `<name>-<10char suffix>`, so match on the name prefix/field."""
    for h in ctl.list_harnesses().get("harnesses", []):
        if h.get("name") == name or h.get("harnessName") == name \
                or h.get("harnessId", "").rsplit("-", 1)[0] == name:
            return h
    return None


def wait_ready(ctl, harness_id, timeout=300):
    for _ in range(timeout // 5):
        h = ctl.get_harness(harnessId=harness_id)["harness"]
        if h["status"] == "READY":
            return h
        if h["status"] in ("FAILED", "DELETING"):
            raise RuntimeError(f"{harness_id}: {h['status']} — {h.get('failureReason')}")
        time.sleep(5)
    raise TimeoutError(f"{harness_id} not READY after {timeout}s")


def create_or_update(ctl, cfg, role_arn, dry):
    name = cfg.pop("harnessName")
    tags = cfg.pop("tags", None)
    cfg = ensure_env(cfg)
    exists = None if dry else existing_harness(ctl, name)

    if dry:
        return {"harness": name, "action": "would create/update",
                "model": cfg["model"]["bedrockModelConfig"]["modelId"],
                "skills": len(cfg.get("skills", [])), "tools": len(cfg.get("tools", []))}

    if exists:
        # UpdateHarness: memory/environmentArtifact/authorizerConfiguration wrap in
        # optionalValue; everything else passes directly. We never send memory here
        # (04_wire_memory.py owns it).
        harness_id = exists["harnessId"]
        wait_ready(ctl, harness_id)  # can't update while CREATING/UPDATING
        update = {k: v for k, v in cfg.items()
                  if k in ("model", "systemPrompt", "tools", "skills", "allowedTools",
                           "maxIterations", "maxTokens", "timeoutSeconds", "truncation",
                           "environment", "environmentVariables")}
        ctl.update_harness(harnessId=harness_id, clientToken=secrets.token_hex(20), **update)
        action = "updated"
    else:
        resp = ctl.create_harness(harnessName=name, executionRoleArn=role_arn,
                                  clientToken=secrets.token_hex(20), **cfg)
        harness_id = resp["harness"]["harnessId"]
        action = "created"

    h = wait_ready(ctl, harness_id)
    if tags:
        ctl.tag_resource(resourceArn=h["arn"], tags=tags)
    return {"harness": name, "harness_id": harness_id, "action": action,
            "status": h["status"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", required=True)
    ap.add_argument("--agent", action="append", choices=AGENTS)
    ap.add_argument("--prod", action="store_true", help="use harness.prod.json variants")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    agents = args.agent or AGENTS

    ctl = boto3.client("bedrock-agentcore-control", region_name=args.region)
    ssm = boto3.client("ssm", region_name=args.region)

    role_arn = None
    if not args.dry_run:
        role_arn = ssm.get_parameter(Name="/llmops/iam/harness_execution_arn")["Parameter"]["Value"]

    results = []
    for agent in agents:
        cfg = load_config(agent, args.prod)
        res = create_or_update(ctl, cfg, role_arn, args.dry_run)
        results.append(res)
        if not args.dry_run:
            ssm.put_parameter(Name=f"/llmops/harness/{agent}", Value=res["harness_id"],
                              Type="String", Overwrite=True)

    print(json.dumps({"results": results, "prod": args.prod, "dry_run": args.dry_run}, indent=2))


if __name__ == "__main__":
    sys.exit(main())
