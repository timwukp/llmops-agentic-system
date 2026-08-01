#!/usr/bin/env python3
"""07_lambdas.py — package and deploy the 5 spine Lambdas + the state machine.

Each Lambda bundle = its handler.py + the contracts (events.py, report.py,
manifest.schema.json) vendored flat so the `except ImportError` fallback path
resolves. Roles come from SSM (01_iam.py); env vars from SSM (03_storage.py).
State machine is created/updated from orchestration/state_machine.asl.json with
${HarnessDriverArn} and ${EventBusName} substituted.

--only selects among ALL targets, Lambdas and non-Lambdas alike (state_machine,
resume_rule); a bare run still deploys everything.

Usage:
  python deploy/07_lambdas.py --region us-east-1 --dry-run
  python deploy/07_lambdas.py --region us-east-1
  python deploy/07_lambdas.py --region us-east-1 --only driver
  python deploy/07_lambdas.py --region us-east-1 --only state_machine   # ASL only
"""
import argparse
import io
import json
import pathlib
import sys
import time
import zipfile

import boto3

REPO = pathlib.Path(__file__).resolve().parent.parent
CONTRACTS = REPO / "pipeline" / "contracts"

LAMBDAS = {
    "driver": {
        "fn": "llmops-harness-driver",
        "src": REPO / "orchestration" / "harness_driver" / "handler.py",
        "role_param": "/llmops/iam/lambda_driver_arn",
        "timeout": 900, "memory": 512,
        "env_keys": ["RUNS_TABLE", "EVENTS_TABLE", "EVENT_BUS", "LLMOPS_SNS_TOPIC", "DATA_BUCKET",
                     "START_FN"],
    },
    "start": {
        "fn": "llmops-start-pipeline",
        "src": REPO / "orchestration" / "start_pipeline" / "handler.py",
        "role_param": "/llmops/iam/lambda_start_arn",
        "timeout": 60, "memory": 256,
        "env_keys": ["RUNS_TABLE", "EVENT_BUS", "DATA_BUCKET", "STATE_MACHINE_ARN"],
    },
    "resume": {
        "fn": "llmops-resume-pipeline",
        "src": REPO / "orchestration" / "resume_pipeline" / "handler.py",
        "role_param": "/llmops/iam/lambda_resume_arn",
        "timeout": 60, "memory": 256,
        "env_keys": ["RUNS_TABLE", "EVENT_BUS"],
    },
    "webhook": {
        "fn": "llmops-webhook",
        "src": REPO / "orchestration" / "webhook" / "handler.py",
        "role_param": "/llmops/iam/lambda_webhook_arn",
        "timeout": 30, "memory": 256,
        "env_keys": ["WEBHOOK_SECRET_ID", "START_PIPELINE_FN"],
    },
    # The auditor's trigger. 08_triggers.py already schedules llmops-finops-daily
    # against this function name, so omitting it here leaves a live EventBridge
    # schedule pointing at a function that does not exist -- a daily failure that
    # surfaces only in the scheduler's own metrics, never in the dashboard.
    "finops": {
        "fn": "llmops-finops-reconcile",
        "src": REPO / "orchestration" / "finops_reconcile" / "handler.py",
        # Its OWN role, not the driver's: iam/lambda_roles.json scopes
        # finops_reconcile to Query/Scan + PutItem + InvokeFunction + Publish, which
        # is strictly narrower than the driver's. Reusing the driver role works and
        # is what a first pass reaches for -- it also hands the auditor every
        # permission the thing it audits has.
        "role_param": "/llmops/iam/lambda_finops_reconcile_arn",
        # 60 s is enough: it lists runs and hands off asynchronously. The auditor's
        # own multi-minute work happens in the harness, not here.
        "timeout": 60, "memory": 256,
        "env_keys": ["RUNS_TABLE", "DATA_BUCKET", "DRIVER_FN",
                     "ESTIMATES_TABLE", "ACTUALS_TABLE", "PROJECT"],
    },
}

STATE_MACHINE_NAME = "llmops-pipeline"


def bundle(src: pathlib.Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(src, "handler.py")
        z.write(CONTRACTS / "events.py", "events.py")
        z.write(CONTRACTS / "report.py", "report.py")
        z.write(CONTRACTS / "manifest.schema.json", "manifest.schema.json")
        # launch_run servicing + approval verification, shared verbatim with the
        # console (its deploy.sh vendors the same file) so the two dispatch paths
        # cannot drift. Vendored into every bundle: only the driver imports it,
        # but a uniform bundle is one less special case in this function.
        z.write(REPO / "orchestration" / "conductor_tools.py", "conductor_tools.py")
    return buf.getvalue()


def env_values(ssm, region, account, keys, extra):
    bucket = ssm.get_parameter(Name="/llmops/storage/bucket")["Parameter"]["Value"]
    base = {
        "RUNS_TABLE": "llmops-pipeline-runs",
        "EVENTS_TABLE": "llmops-stage-events",
        "EVENT_BUS": "llmops-pipeline",
        "DATA_BUCKET": bucket,
        "LLMOPS_SNS_TOPIC": f"arn:aws:sns:{region}:{account}:llmops-escalations",
        "STATE_MACHINE_ARN": f"arn:aws:states:{region}:{account}:stateMachine:{STATE_MACHINE_NAME}",
        "WEBHOOK_SECRET_ID": "llmops/webhook",
        "START_PIPELINE_FN": "llmops-start-pipeline",
        "START_FN": "llmops-start-pipeline",   # driver's launch_run dispatch target
        "DRIVER_FN": "llmops-harness-driver",
        "ESTIMATES_TABLE": "llmops-cost-estimates",
        "ACTUALS_TABLE": "llmops-cost-actuals",
        "PROJECT": "llmops-agentic-system",
    }
    base.update(extra or {})
    return {k: base[k] for k in keys}


def deploy_lambda(lam, ssm, region, account, key, cfg, dry):
    role_arn = None if dry else ssm.get_parameter(Name=cfg["role_param"])["Parameter"]["Value"]
    env = env_values(ssm, region, account, cfg["env_keys"], None) if not dry else {}
    if dry:
        return {"lambda": cfg["fn"], "would": "create/update", "env_keys": cfg["env_keys"]}
    code = bundle(cfg["src"])
    try:
        lam.get_function(FunctionName=cfg["fn"])
        lam.update_function_code(FunctionName=cfg["fn"], ZipFile=code)
        waiter = lam.get_waiter("function_updated_v2")
        waiter.wait(FunctionName=cfg["fn"])
        # Role goes in the UPDATE too, not just the create. Without it a role change
        # in LAMBDAS above applies only to functions that do not exist yet: every
        # re-run reports "updated" while the live function keeps whatever role it was
        # born with. That is silent in both directions -- a tightened role never takes
        # effect, and nothing ever says so.
        lam.update_function_configuration(
            FunctionName=cfg["fn"], Role=role_arn,
            Timeout=cfg["timeout"], MemorySize=cfg["memory"],
            Environment={"Variables": env})
        action = "updated"
    except lam.exceptions.ResourceNotFoundException:
        lam.create_function(
            FunctionName=cfg["fn"], Runtime="python3.12", Role=role_arn,
            Handler="handler.handler", Code={"ZipFile": code},
            Timeout=cfg["timeout"], MemorySize=cfg["memory"],
            Environment={"Variables": env},
            Tags={"project": "llmops-agentic-system"})
        action = "created"
    return {"lambda": cfg["fn"], "action": action}


def deploy_state_machine(sfn, ssm, region, account, dry):
    asl = (REPO / "orchestration" / "state_machine.asl.json").read_text()
    driver_arn = f"arn:aws:lambda:{region}:{account}:function:llmops-harness-driver"
    asl = asl.replace("${HarnessDriverArn}", driver_arn)
    asl = asl.replace("${EventBusName}", "llmops-pipeline")
    if dry:
        # json.loads only proves it is JSON. ASL rejects plenty of valid JSON -- an
        # unsupported field, a bad JSONPath, an unknown SDK integration -- and does so
        # at UpdateStateMachine time, i.e. in the middle of a real deploy. The
        # ValidateStateMachineDefinition API is read-only and creates nothing, so the
        # dry run can make the claim it was already printing.
        json.loads(asl)
        try:
            checked = sfn.validate_state_machine_definition(definition=asl,
                                                            type="STANDARD")
        except Exception as exc:  # no credentials / no network: say so, do not claim
            return {"state_machine": STATE_MACHINE_NAME, "would": "create/update",
                    "asl": "json-parses; ASL NOT validated",
                    "validator_unreachable": f"{type(exc).__name__}: {exc}"}
        diags = [f"[{d['severity']}] {d['code']} {d.get('location', '')}: {d['message']}"
                 for d in checked.get("diagnostics", [])]
        return {"state_machine": STATE_MACHINE_NAME, "would": "create/update",
                "asl": checked["result"], "diagnostics": diags}
    # The state machine's own role, published by 01_iam.py from iam/sfn_execution_role.json.
    # The legacy /llmops/iam/sfn_arn name is still read as a fallback for accounts
    # deployed before the role was declared in-repo; the start role is the last resort
    # and is wrong (it cannot write the runs table), so it fails loudly at MarkRunFailed
    # rather than silently granting the state machine the wrong identity.
    for param in ("/llmops/iam/sfn_execution_arn", "/llmops/iam/sfn_arn",
                  "/llmops/iam/lambda_start_arn"):
        try:
            role_arn = ssm.get_parameter(Name=param)["Parameter"]["Value"]
            break
        except ssm.exceptions.ParameterNotFound:
            continue
    else:
        raise RuntimeError("no state machine role in SSM — run deploy/01_iam.py first")
    sm_arn = f"arn:aws:states:{region}:{account}:stateMachine:{STATE_MACHINE_NAME}"
    try:
        sfn.describe_state_machine(stateMachineArn=sm_arn)
        sfn.update_state_machine(stateMachineArn=sm_arn, definition=asl, roleArn=role_arn)
        action = "updated"
    except sfn.exceptions.StateMachineDoesNotExist:
        sfn.create_state_machine(name=STATE_MACHINE_NAME, definition=asl,
                                 roleArn=role_arn, type="STANDARD",
                                 tags=[{"key": "project", "value": "llmops-agentic-system"}])
        action = "created"
    return {"state_machine": STATE_MACHINE_NAME, "action": action}


def ensure_resume_rule(events, lam, region, account, dry):
    """EventBridge rule: SageMaker Training Job State Change -> resume lambda.
    Default bus (SageMaker service events land there, not on custom buses)."""
    rule = "llmops-sagemaker-job-state"
    pattern = {
        "source": ["aws.sagemaker"],
        "detail-type": ["SageMaker Training Job State Change"],
        "detail": {"TrainingJobStatus": ["Completed", "Failed", "Stopped"]},
    }
    if dry:
        return {"rule": rule, "would": "put_rule + target + permission"}
    events.put_rule(Name=rule, EventPattern=json.dumps(pattern), State="ENABLED",
                    Description="Resume llmops pipeline when a training job finishes")
    fn_arn = f"arn:aws:lambda:{region}:{account}:function:llmops-resume-pipeline"
    events.put_targets(Rule=rule, Targets=[{"Id": "resume", "Arn": fn_arn}])
    try:
        lam.add_permission(FunctionName="llmops-resume-pipeline",
                           StatementId="eventbridge-sagemaker-state",
                           Action="lambda:InvokeFunction",
                           Principal="events.amazonaws.com",
                           SourceArn=f"arn:aws:events:{region}:{account}:rule/{rule}")
    except lam.exceptions.ResourceConflictException:
        pass  # permission already exists
    return {"rule": rule, "action": "ensured"}


# --only selects among ALL of this script's targets, not just the Lambdas. The state
# machine and the resume rule used to deploy unconditionally on every run, which made
# --only the opposite of what it says: `--only driver` shipped the driver AND the ASL,
# and there was no way to ship the ASL alone. Both directions bit. The ASL change that
# added MarkRunDone could not be deployed without also shipping a driver whose redeploy
# is deliberately held back pending an IAM widen; and a driver-only redeploy silently
# published whatever the working tree's ASL happened to say. A targeted deploy has to
# mean what it claims, because the whole reason to reach for --only is blast radius.
NON_LAMBDA_TARGETS = ("state_machine", "resume_rule")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", required=True)
    ap.add_argument("--only", action="append",
                    choices=list(LAMBDAS) + list(NON_LAMBDA_TARGETS))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    lam = boto3.client("lambda", region_name=args.region)
    ssm = boto3.client("ssm", region_name=args.region)
    sfn = boto3.client("stepfunctions", region_name=args.region)
    events = boto3.client("events", region_name=args.region)
    account = "" if args.dry_run else boto3.client("sts", region_name=args.region) \
        .get_caller_identity()["Account"]

    targets = args.only or list(LAMBDAS) + list(NON_LAMBDA_TARGETS)
    results = [deploy_lambda(lam, ssm, args.region, account, k, LAMBDAS[k], args.dry_run)
               for k in targets if k in LAMBDAS]
    if "state_machine" in targets:
        results.append(deploy_state_machine(sfn, ssm, args.region, account, args.dry_run))
    if "resume_rule" in targets:
        results.append(ensure_resume_rule(events, lam, args.region, account, args.dry_run))
    print(json.dumps({"results": results, "targets": targets,
                      "dry_run": args.dry_run}, indent=2, default=str))


if __name__ == "__main__":
    sys.exit(main())
