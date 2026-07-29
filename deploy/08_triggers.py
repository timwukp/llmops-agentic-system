#!/usr/bin/env python3
"""08_triggers.py — wire the four pipeline triggers to start-pipeline.

1. EventBridge Scheduler: cron schedule (default DISABLED — enable when nightly
   runs are wanted) invoking llmops-start-pipeline with trigger_source=scheduler.
2. Webhook: API Gateway HTTP API -> llmops-webhook Lambda (HMAC verified inside);
   secret created in Secrets Manager if absent.
3. Admin API: POST /runs on the same HTTP API -> llmops-start-pipeline directly
   (IAM-auth route; the ops console or awscurl calls it).
4. GitHub Actions: no AWS resource needed beyond the OIDC role — emits the
   workflow file to .github/workflows/run-pipeline.yml separately (repo side).

Usage:
  python deploy/08_triggers.py --region us-east-1 --dry-run
  python deploy/08_triggers.py --region us-east-1
  python deploy/08_triggers.py --region us-east-1 --enable-schedule  # nightly on
"""
import argparse
import json
import secrets as pysecrets
import sys

import boto3

SCHEDULE_NAME = "llmops-nightly"
API_NAME = "llmops-triggers"
SECRET_ID = "llmops/webhook"


def ensure_schedule(sched, lam_arn, role_arn, enable, dry):
    state = "ENABLED" if enable else "DISABLED"
    if dry:
        return {"schedule": SCHEDULE_NAME, "would": f"create/update cron(0 3 * * ? *) {state}"}
    body = dict(
        Name=SCHEDULE_NAME,
        ScheduleExpression="cron(0 3 * * ? *)",  # 03:00 UTC nightly
        ScheduleExpressionTimezone="UTC",
        FlexibleTimeWindow={"Mode": "FLEXIBLE", "MaximumWindowInMinutes": 15},
        Target={
            "Arn": lam_arn,
            "RoleArn": role_arn,
            "Input": json.dumps({"trigger_source": "scheduler"}),
        },
        State=state,
    )
    try:
        sched.create_schedule(**body)
        action = "created"
    except sched.exceptions.ConflictException:
        sched.update_schedule(**body)
        action = "updated"
    return {"schedule": SCHEDULE_NAME, "action": action, "state": state}


def ensure_scheduler_role(iam, region, account, dry):
    """Role EventBridge Scheduler assumes to invoke start-pipeline."""
    name = "llmops-scheduler-invoke"
    if dry:
        return None, {"role": name, "would": "create"}
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "scheduler.amazonaws.com"},
        "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": account}}}]}
    policy = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Action": "lambda:InvokeFunction",
        "Resource": f"arn:aws:lambda:{region}:{account}:function:llmops-start-pipeline"}]}
    try:
        iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust),
                        Tags=[{"Key": "project", "Value": "llmops-agentic-system"}])
        action = "created"
    except iam.exceptions.EntityAlreadyExistsException:
        action = "exists"
    iam.put_role_policy(RoleName=name, PolicyName="InvokeStartPipeline",
                        PolicyDocument=json.dumps(policy))
    arn = iam.get_role(RoleName=name)["Role"]["Arn"]
    return arn, {"role": name, "action": action}


def ensure_secret(sm, dry):
    if dry:
        return {"secret": SECRET_ID, "would": "create if absent"}
    try:
        sm.describe_secret(SecretId=SECRET_ID)
        return {"secret": SECRET_ID, "action": "exists"}
    except sm.exceptions.ResourceNotFoundException:
        sm.create_secret(Name=SECRET_ID, SecretString=pysecrets.token_hex(32),
                         Description="HMAC key for the llmops webhook trigger",
                         Tags=[{"Key": "project", "Value": "llmops-agentic-system"}])
        return {"secret": SECRET_ID, "action": "created"}


def ensure_api(apigw, lam, region, account, dry):
    """HTTP API: POST /webhook -> webhook λ (no auth; HMAC inside),
    POST /runs -> start-pipeline λ (IAM auth)."""
    if dry:
        return {"api": API_NAME, "would": "create HTTP API with /webhook + /runs routes"}
    apis = {a["Name"]: a for a in apigw.get_apis()["Items"]}
    if API_NAME in apis:
        api = apis[API_NAME]
        api_id = api["ApiId"]
        action = "exists"
    else:
        api = apigw.create_api(Name=API_NAME, ProtocolType="HTTP",
                               Tags={"project": "llmops-agentic-system"})
        api_id = api["ApiId"]
        action = "created"
        for fn, route, auth in (("llmops-webhook", "POST /webhook", "NONE"),
                                ("llmops-start-pipeline", "POST /runs", "AWS_IAM")):
            fn_arn = f"arn:aws:lambda:{region}:{account}:function:{fn}"
            integ = apigw.create_integration(
                ApiId=api_id, IntegrationType="AWS_PROXY",
                IntegrationUri=fn_arn, PayloadFormatVersion="2.0")
            apigw.create_route(ApiId=api_id, RouteKey=route,
                               AuthorizationType=auth,
                               Target=f"integrations/{integ['IntegrationId']}")
            try:
                lam.add_permission(
                    FunctionName=fn, StatementId=f"apigw-{route.split()[1].strip('/')}",
                    Action="lambda:InvokeFunction", Principal="apigateway.amazonaws.com",
                    SourceArn=f"arn:aws:execute-api:{region}:{account}:{api_id}/*")
            except lam.exceptions.ResourceConflictException:
                pass
        apigw.create_stage(ApiId=api_id, StageName="$default", AutoDeploy=True)
    return {"api": API_NAME, "action": action, "api_id": api_id,
            "endpoint": api.get("ApiEndpoint", "?")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", required=True)
    ap.add_argument("--enable-schedule", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    region = args.region
    iam = boto3.client("iam", region_name=region)
    sched = boto3.client("scheduler", region_name=region)
    sm = boto3.client("secretsmanager", region_name=region)
    apigw = boto3.client("apigatewayv2", region_name=region)
    lam = boto3.client("lambda", region_name=region)
    ssm = boto3.client("ssm", region_name=region)
    account = "" if args.dry_run else boto3.client("sts").get_caller_identity()["Account"]

    results = []
    role_arn, r = ensure_scheduler_role(iam, region, account, args.dry_run)
    results.append(r)
    lam_arn = f"arn:aws:lambda:{region}:{account}:function:llmops-start-pipeline"
    results.append(ensure_schedule(sched, lam_arn, role_arn, args.enable_schedule, args.dry_run))
    results.append(ensure_secret(sm, args.dry_run))
    api_res = ensure_api(apigw, lam, region, account, args.dry_run)
    results.append(api_res)
    if not args.dry_run and api_res.get("endpoint") not in (None, "?"):
        ssm.put_parameter(Name="/llmops/triggers/api_endpoint", Value=api_res["endpoint"],
                          Type="String", Overwrite=True)
    print(json.dumps({"results": results, "dry_run": args.dry_run}, indent=2, default=str))


if __name__ == "__main__":
    sys.exit(main())
