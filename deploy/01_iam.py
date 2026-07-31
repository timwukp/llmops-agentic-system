#!/usr/bin/env python3
"""01_iam.py — provision least-privilege IAM roles for llmops-agentic-system (idempotent).

Reads the policy documents in deploy/iam/ (which contain ONLY placeholders — no account
ids are ever committed), substitutes <ACCOUNT_ID>/<REGION>/<DATA_BUCKET>/<MEMORY_ID> at
deploy time (account id from sts get-caller-identity, or --account-id for offline
dry-runs), then create-or-updates:

  llmops-harness-execution     <- iam/harness_execution_role.json  (all 5 worker harnesses)
  llmops-sagemaker-execution   <- iam/sagemaker_execution_role.json (passed to SageMaker)
  llmops-lambda-driver         <- iam/lambda_roles.json roles.driver  (+ shared statements)
  llmops-lambda-start          <- iam/lambda_roles.json roles.start
  llmops-lambda-resume         <- iam/lambda_roles.json roles.resume
  llmops-lambda-webhook        <- iam/lambda_roles.json roles.webhook
  llmops-sfn-execution         <- iam/sfn_execution_role.json (the state machine itself)

Each role gets one inline policy named `llmops-permissions` and tag project=llmops-agentic-system.
Role ARNs are published to SSM under /llmops/iam/<role>_arn for the later deploy steps.

BYO-memory statements in the harness role are dropped unless --memory-id is given
(deploy/wire_memory.py re-grants them per-Memory after the shared memory exists).

--dry-run prints a diff of what would change and performs no writes. It needs no AWS
calls beyond STS (skipped entirely when --account-id is given); reads of existing roles
are attempted best-effort and degrade to "would create" when credentials are absent.

Usage:
  python deploy/01_iam.py --region us-east-1 [--bucket NAME] [--memory-id ID] [--dry-run]
  python deploy/01_iam.py --region us-east-1 --account-id 123456789012 --dry-run   # offline
"""
import argparse
import difflib
import json
import sys
from pathlib import Path

import boto3

TAG_KEY, TAG_VAL = "project", "llmops-agentic-system"
IAM_DIR = Path(__file__).resolve().parent / "iam"
INLINE_POLICY_NAME = "llmops-permissions"


def load_doc(name):
    with open(IAM_DIR / name) as f:
        return json.load(f)


def substitute(obj, mapping):
    """Recursively replace <PLACEHOLDER> tokens in every string of a JSON structure."""
    if isinstance(obj, str):
        for k, v in mapping.items():
            obj = obj.replace(k, v)
        return obj
    if isinstance(obj, list):
        return [substitute(x, mapping) for x in obj]
    if isinstance(obj, dict):
        return {k: substitute(v, mapping) for k, v in obj.items() if k != "_comment"}
    return obj


def strip_byo_memory(policy):
    """Remove BYOMemory* statements when no --memory-id is supplied (wire_memory.py adds them later)."""
    policy["Statement"] = [s for s in policy["Statement"]
                           if not s.get("Sid", "").startswith("BYOMemory")]
    return policy


def build_role_specs(mapping, memory_id):
    """Return {role_name: {"trust": doc, "policy": doc}} fully substituted."""
    specs = {}

    harness = load_doc("harness_execution_role.json")
    hp = harness["permissionsPolicy"]
    if not memory_id:
        hp = strip_byo_memory(hp)
    specs["llmops-harness-execution"] = {
        "trust": substitute(harness["trustPolicy"], mapping),
        "policy": substitute(hp, mapping),
    }

    sm = load_doc("sagemaker_execution_role.json")
    specs["llmops-sagemaker-execution"] = {
        "trust": substitute(sm["trustPolicy"], mapping),
        "policy": substitute(sm["permissionsPolicy"], mapping),
    }

    # The state machine's own role. Declared here rather than in lambda_roles.json
    # because its trust policy names states.amazonaws.com, not lambda.
    sfn = load_doc("sfn_execution_role.json")
    specs["llmops-sfn-execution"] = {
        "trust": substitute(sfn["trustPolicy"], mapping),
        "policy": substitute(sfn["permissionsPolicy"], mapping),
    }

    lam = load_doc("lambda_roles.json")
    trust = substitute(lam["trustPolicy"], mapping)
    shared = substitute(lam["sharedStatements"], mapping)
    for key, role in lam["roles"].items():
        pol = substitute(role["permissionsPolicy"], mapping)
        pol["Statement"] = pol["Statement"] + shared
        specs[f"llmops-lambda-{key}"] = {"trust": trust, "policy": pol}

    return specs


def get_existing(iam, role_name):
    """Best-effort read of the current role state; None means 'absent or unreadable'."""
    try:
        role = iam.get_role(RoleName=role_name)["Role"]
        trust = role["AssumeRolePolicyDocument"]
    except Exception:
        return None, None
    try:
        pol = iam.get_role_policy(RoleName=role_name, PolicyName=INLINE_POLICY_NAME)["PolicyDocument"]
    except Exception:
        pol = None
    return trust, pol


def show_diff(label, current, desired):
    cur = json.dumps(current, indent=2, sort_keys=True).splitlines() if current else []
    des = json.dumps(desired, indent=2, sort_keys=True).splitlines()
    diff = list(difflib.unified_diff(cur, des, fromfile=f"{label} (current)",
                                     tofile=f"{label} (desired)", lineterm=""))
    if not diff:
        print(f"    {label}: no change")
        return False
    for line in diff:
        print(f"    {line}")
    return True


def ensure_role(iam, name, spec, dry):
    trust_now, policy_now = get_existing(iam, name) if iam else (None, None)
    action = "update" if trust_now is not None else "create"
    print(f"  [{action}] role {name}")
    changed = False
    changed |= show_diff("trust policy", trust_now, spec["trust"])
    changed |= show_diff("inline policy", policy_now, spec["policy"])
    if dry:
        return changed
    if trust_now is None:
        iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(spec["trust"]),
            Description=f"{TAG_VAL} least-privilege role (managed by deploy/01_iam.py)",
            Tags=[{"Key": TAG_KEY, "Value": TAG_VAL}],
        )
        iam.get_waiter("role_exists").wait(RoleName=name)
    elif json.dumps(trust_now, sort_keys=True) != json.dumps(spec["trust"], sort_keys=True):
        iam.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(spec["trust"]))
    iam.put_role_policy(RoleName=name, PolicyName=INLINE_POLICY_NAME,
                        PolicyDocument=json.dumps(spec["policy"]))
    iam.tag_role(RoleName=name, Tags=[{"Key": TAG_KEY, "Value": TAG_VAL}])
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", required=True)
    ap.add_argument("--account-id", help="skip STS (offline dry-run) and use this account id")
    ap.add_argument("--bucket", help="data bucket name (default llmops-agentic-<acct>-<region>)")
    ap.add_argument("--memory-id", help="BYO AgentCore Memory id; omit until wire_memory.py runs")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    account_id = args.account_id
    if not account_id:
        account_id = boto3.client("sts", region_name=args.region).get_caller_identity()["Account"]
    bucket = args.bucket or f"llmops-agentic-{account_id}-{args.region}"

    mapping = {
        "<ACCOUNT_ID>": account_id,
        "<REGION>": args.region,
        "<DATA_BUCKET>": bucket,
    }
    if args.memory_id:
        mapping["<MEMORY_ID>"] = args.memory_id

    specs = build_role_specs(mapping, args.memory_id)

    iam = None
    if not args.dry_run:
        iam = boto3.client("iam", region_name=args.region)
    else:
        try:  # best-effort: show a real diff when credentials exist, else "would create"
            iam = boto3.client("iam", region_name=args.region)
            iam.get_role(RoleName="llmops-harness-execution")
        except Exception:
            iam = None

    print(f"{'DRY-RUN — ' if args.dry_run else ''}region={args.region} "
          f"bucket={bucket} roles={len(specs)}")
    any_change = False
    for name, spec in specs.items():
        any_change |= ensure_role(iam, name, spec, args.dry_run)

    if not args.dry_run:
        ssm = boto3.client("ssm", region_name=args.region)
        for name in specs:
            arn = iam.get_role(RoleName=name)["Role"]["Arn"]
            key = name.replace("llmops-", "").replace("-", "_")
            ssm.put_parameter(Name=f"/llmops/iam/{key}_arn", Value=arn,
                              Type="String", Overwrite=True)
            print(f"  ssm /llmops/iam/{key}_arn = {arn}")

    print(json.dumps({"roles": sorted(specs), "dry_run": args.dry_run,
                      "changes_pending" if args.dry_run else "changed": any_change}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
