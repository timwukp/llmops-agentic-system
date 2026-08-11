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
  llmops-lambda-resurrector       <- iam/lambda_roles.json roles.resurrector
  llmops-lambda-monitor_sweep     <- iam/lambda_roles.json roles.monitor_sweep
  llmops-lambda-finops_reconcile  <- iam/lambda_roles.json roles.finops_reconcile
  llmops-sfn-execution         <- iam/sfn_execution_role.json (the state machine itself)

(This list is prose; ROLE_NAMES below is derived from iam/ and is the authoritative set.)

Each role gets one inline policy named `llmops-permissions` and tag project=llmops-agentic-system.
Role ARNs are published to SSM under /llmops/iam/<role>_arn for the later deploy steps.

SINGLE-REGION. The role names are global constants but their policies embed <REGION>, so
deploying a second region into the same account would replace each inline policy and strip
the first region's ARNs. A `llmops:region` tag records the owner and a pre-flight refuses
such a deploy (exit 2) rather than half-applying it; --force-region-takeover overrides and
breaks the other region. Real multi-region needs distinct role names, not this script.

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

#: Role tag recording which region's ARNs the inline policy was built for.
#
# IAM is GLOBAL and these role names are constants, but the policies are not global:
# 71 resource ARNs across deploy/iam/*.json carry <REGION>. put_role_policy REPLACES
# by name, so running this script for a second region rewrote every role's policy with
# region-2 ARNs and silently stripped region 1's permissions -- an outage in a running
# deployment, reported as a successful deploy, with the diff scrolling past as ordinary
# "update" output.
#
# The tempting fix -- one inline policy per region -- cannot work: IAM caps the
# AGGREGATE inline policy size for a role at 10,240 characters and the harness policy
# alone substitutes to ~7.4k, so two regions do not fit on one role at all. Genuine
# multi-region therefore needs distinct role NAMES (the --name-prefix work, P3 in the
# audit), which is a larger change than this script. Until then the honest behaviour is
# to refuse: a deploy that would take a role away from another region stops and says so.
REGION_TAG_KEY = "llmops:region"


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
        return {k: substitute(v, mapping) for k, v in obj.items() if not k.startswith("_comment")}
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


#: Every role this script provisions. Derived by building the specs with an empty
#: substitution map (substitute() is the identity for an empty mapping), so a role added
#: to deploy/iam/ is covered by the region-conflict pre-flight automatically instead of
#: being quietly skipped by a hand-maintained list that nobody remembers to extend.
ROLE_NAMES = tuple(build_role_specs({}, None))


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


def role_region(iam, role_name):
    """The region this role's inline policy was last built for, or None if unknown.

    None covers three cases that all mean "do not block": the role does not exist, it
    predates this tag, or we cannot read it. Only a tag that names a DIFFERENT region is
    evidence of a conflict, so an unreadable role never blocks a first deploy.
    """
    try:
        tags = iam.get_role(RoleName=role_name)["Role"].get("Tags", [])
    except Exception:
        return None
    for t in tags:
        if t["Key"] == REGION_TAG_KEY:
            return t["Value"]
    return None


def find_region_conflicts(iam, role_names, region):
    """[(role, other_region)] for roles already built for a region that is not this one.

    A pre-flight over EVERY role, run before the first write. Checking inside the
    per-role loop would be too late to help: by the time role 7 raised the alarm,
    put_role_policy would already have replaced roles 1-6 and taken those permissions
    away from the other region -- the exact outage the check exists to prevent, now
    half-committed and with no record of what the previous documents were.
    """
    if iam is None:
        return []
    out = []
    for name in role_names:
        other = role_region(iam, name)
        if other and other != region:
            out.append((name, other))
    return out


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


def ensure_role(iam, name, spec, dry, region):
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
            Tags=[{"Key": TAG_KEY, "Value": TAG_VAL},
                  {"Key": REGION_TAG_KEY, "Value": region}],
        )
        iam.get_waiter("role_exists").wait(RoleName=name)
    elif json.dumps(trust_now, sort_keys=True) != json.dumps(spec["trust"], sort_keys=True):
        iam.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(spec["trust"]))
    iam.put_role_policy(RoleName=name, PolicyName=INLINE_POLICY_NAME,
                        PolicyDocument=json.dumps(spec["policy"]))
    # Stamped AFTER the policy, never before. If the policy write fails the tag must
    # still name whoever owns the document that is actually attached: a tag claiming a
    # region whose statements never landed would block that region's own redeploy while
    # leaving the other region's permissions in place -- refusing the one deploy that
    # would have fixed things. This order fails the harmless way instead: the tag lags,
    # and the owning region's next run corrects it.
    iam.tag_role(RoleName=name, Tags=[{"Key": TAG_KEY, "Value": TAG_VAL},
                                      {"Key": REGION_TAG_KEY, "Value": region}])
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", required=True)
    ap.add_argument("--account-id", help="skip STS (offline dry-run) and use this account id")
    ap.add_argument("--bucket", help="data bucket name (default llmops-agentic-<acct>-<region>)")
    ap.add_argument("--memory-id", help="BYO AgentCore Memory id; omit until wire_memory.py runs")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-region-takeover", action="store_true",
                    help="deploy even though the roles belong to another region — this "
                         "STRIPS that region's permissions and breaks its running "
                         "deployment; only correct when that region is already gone")
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

    conflicts = find_region_conflicts(iam, list(specs), args.region)
    if conflicts and args.force_region_takeover:
        for name, other in conflicts:
            print(f"  [takeover] {name}: was {other}, now {args.region} — "
                  f"{other}'s permissions are being removed")
        conflicts = []
    if conflicts:
        print(f"\nREFUSING: these roles are global and currently carry {args.region}-"
              "incompatible policies built for another region:\n", file=sys.stderr)
        for name, other in conflicts:
            print(f"  {name}: built for {other}", file=sys.stderr)
        print(f"\nDeploying {args.region} would replace the inline policy on each and "
              f"strip every {conflicts[0][1]} ARN from it, breaking that deployment "
              "with no warning. Two regions cannot share one role: IAM caps a role's "
              "total inline policy at 10,240 chars and the harness policy alone is "
              "~7.4k, so per-region policy names do not fit either. Run distinct role "
              f"names per region, or tear down {conflicts[0][1]} first "
              f"(--force-region-takeover overrides, and WILL break {conflicts[0][1]}).",
              file=sys.stderr)
        return 2

    any_change = False
    for name, spec in specs.items():
        any_change |= ensure_role(iam, name, spec, args.dry_run, args.region)

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
