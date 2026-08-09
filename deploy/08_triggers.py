#!/usr/bin/env python3
"""08_triggers.py — wire the four pipeline triggers to start-pipeline, plus the
daily FinOps reconciliation schedule.

1. EventBridge Scheduler: cron schedule (default DISABLED — enable when nightly
   runs are wanted) invoking llmops-start-pipeline with trigger_source=scheduler.
1b. EventBridge Scheduler: llmops-finops-daily, cron(0 9 * * ? *), default ENABLED —
   invokes llmops-finops-reconcile. Enabled by default because it only reads billing
   APIs, and because a day skipped is a period left provisional.
1c. EventBridge Scheduler: llmops-monitor-sweep-daily, cron(0 8 * * ? *), default ENABLED —
   invokes llmops-monitor-sweep (the llmops_monitor `sweep` task). Also $0 to run, and
   scheduled rather than in the state machine because an orphaned endpoint belongs to a run
   that has already ended: no live agent is left to find it.
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
FINOPS_SCHEDULE_NAME = "llmops-finops-daily"
SWEEP_SCHEDULE_NAME = "llmops-monitor-sweep-daily"
RESURRECTOR_SCHEDULE_NAME = "llmops-resurrector-15min"
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


def ensure_finops_schedule(sched, lam_arn, role_arn, enable, dry):
    """Daily cost reconciliation — ENABLED by default, unlike the nightly pipeline.

    The nightly run is off by default because it spends GPU money. This one only reads
    billing APIs, and its value comes from running every day: a period first read while
    Cost Explorer still marks it Estimated stays provisional until something goes back
    for it. Skipping days is how a dashboard ends up quoting provisional numbers.

    09:00 UTC, ~9 h after the nightly pipeline's 03:00 start, so a run launched last
    night has finished and its usage has begun landing in Cost Explorer.

    NON-FLEXIBLE window on purpose: the reconcile Lambda derives its default period
    from the current date, so a job drifting past midnight UTC would silently read a
    different day than the one it was scheduled for.
    """
    state = "ENABLED" if enable else "DISABLED"
    if dry:
        return {"schedule": FINOPS_SCHEDULE_NAME,
                "would": f"create/update cron(0 9 * * ? *) {state}"}
    body = dict(
        Name=FINOPS_SCHEDULE_NAME,
        ScheduleExpression="cron(0 9 * * ? *)",  # 09:00 UTC daily
        ScheduleExpressionTimezone="UTC",
        FlexibleTimeWindow={"Mode": "OFF"},
        Target={
            "Arn": lam_arn,
            "RoleArn": role_arn,
            # Empty input: the Lambda picks the newly-settled period plus any period
            # still provisional. Pinning a period here would freeze the schedule to a
            # date and defeat the re-settlement pass.
            "Input": json.dumps({"task": "reconcile"}),
            "RetryPolicy": {"MaximumRetryAttempts": 2,
                            "MaximumEventAgeInSeconds": 3600},
        },
        State=state,
    )
    try:
        sched.create_schedule(**body)
        action = "created"
    except sched.exceptions.ConflictException:
        sched.update_schedule(**body)
        action = "updated"
    return {"schedule": FINOPS_SCHEDULE_NAME, "action": action, "state": state}


def ensure_sweep_schedule(sched, lam_arn, role_arn, enable, dry):
    """Daily orphan sweep — ENABLED by default, for the same reason as the finops one.

    It costs nothing to run (list-endpoints, list-tags and metric reads are $0) and its
    whole value is that it runs when nobody remembers to. The orphan it exists to catch is
    by definition one no run is watching: the one standing endpoint this account carried was
    ``jumpstart-dft-hf-asr-whisper-large-v2``, InService 2024-04-11 → deleted 2026-08-02,
    untagged, and no run was ever going to be responsible for it. A sweep that only ran
    inside a healthy run could never find an endpoint left behind by a run that crashed —
    which is precisely the endpoint that bills for a month. That the account is clean today
    is the argument FOR keeping this schedule enabled, not against: the finding it produced
    is the evidence that nothing else in the account was going to produce one.

    08:00 UTC, one hour ahead of the finops reconcile: the sweep reports what is still
    standing, so it is better read before the auditor tallies what was spent. Same
    NON-FLEXIBLE window as the reconcile, and for the same reason — the Lambda derives its
    sweep id from the current date, so a job drifting past midnight UTC would file its
    findings under a different day than the one it was scheduled for.
    """
    state = "ENABLED" if enable else "DISABLED"
    if dry:
        return {"schedule": SWEEP_SCHEDULE_NAME,
                "would": f"create/update cron(0 8 * * ? *) {state}"}
    body = dict(
        Name=SWEEP_SCHEDULE_NAME,
        ScheduleExpression="cron(0 8 * * ? *)",  # 08:00 UTC daily
        ScheduleExpressionTimezone="UTC",
        FlexibleTimeWindow={"Mode": "OFF"},
        Target={
            "Arn": lam_arn,
            "RoleArn": role_arn,
            "Input": json.dumps({"task": "sweep"}),
            "RetryPolicy": {"MaximumRetryAttempts": 2,
                            "MaximumEventAgeInSeconds": 3600},
        },
        State=state,
    )
    try:
        sched.create_schedule(**body)
        action = "created"
    except sched.exceptions.ConflictException:
        sched.update_schedule(**body)
        action = "updated"
    return {"schedule": SWEEP_SCHEDULE_NAME, "action": action, "state": state}


def ensure_resurrector_schedule(sched, lam_arn, role_arn, enable, dry):
    """Every-15-minutes dead-driver check — ENABLED by default.

    This is the liveness half of the driver's heartbeat contract (PR #67): the driver
    stamps driver_beat_at on the run row every turn, and this schedule is the only
    thing that reads the stamp. It exists because the driver's turn handoff is an
    ASYNC self-invoke that Lambda may drop (observed 2026-08-08, AsyncEventsDropped=1:
    run 68cfa9c8 sat dead nine hours at 4/55 tasks with its token parked, Step
    Functions RUNNING, and nothing anywhere whose job it was to notice). It is also
    what makes AgentCore's 8-hour session maxLifetime a non-event on 8-12h stages:
    sessions may die; the resurrector re-invokes; a fresh session resumes from S3.

    15 minutes because the stale threshold is 20: a beat can be at most stale+15min
    old before someone acts, bounding a dead driver's silence at ~35 minutes instead
    of nine hours. Idle cost is one Scan over tens of rows, ~$0. FLEXIBLE window is
    fine — unlike the date-keyed sweeps, nothing here derives identity from the clock.
    """
    state = "ENABLED" if enable else "DISABLED"
    if dry:
        return {"schedule": RESURRECTOR_SCHEDULE_NAME,
                "would": f"create/update rate(15 minutes) {state}"}
    body = dict(
        Name=RESURRECTOR_SCHEDULE_NAME,
        ScheduleExpression="rate(15 minutes)",
        FlexibleTimeWindow={"Mode": "FLEXIBLE", "MaximumWindowInMinutes": 5},
        Target={
            "Arn": lam_arn,
            "RoleArn": role_arn,
            "Input": json.dumps({"trigger_source": "resurrector-schedule"}),
            "RetryPolicy": {"MaximumRetryAttempts": 2,
                            "MaximumEventAgeInSeconds": 600},
        },
        State=state,
    )
    try:
        sched.create_schedule(**body)
        action = "created"
    except sched.exceptions.ConflictException:
        sched.update_schedule(**body)
        action = "updated"
    return {"schedule": RESURRECTOR_SCHEDULE_NAME, "action": action, "state": state}


def ensure_scheduler_role(iam, region, account, dry):
    """Role EventBridge Scheduler assumes to invoke the three scheduled functions.

    The Resource list is exhaustive by design: every schedule this file creates must have
    its target named here, because a schedule pointing at a function the role may not
    invoke fails silently in the scheduler's own metrics and never in the dashboard —
    indistinguishable from a schedule that ran and found nothing.
    """
    name = "llmops-scheduler-invoke"
    if dry:
        return None, {"role": name, "would": "create"}
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "scheduler.amazonaws.com"},
        "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": account}}}]}
    policy = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Action": "lambda:InvokeFunction",
        "Resource": [
            f"arn:aws:lambda:{region}:{account}:function:llmops-start-pipeline",
            f"arn:aws:lambda:{region}:{account}:function:llmops-finops-reconcile",
            f"arn:aws:lambda:{region}:{account}:function:llmops-monitor-sweep",
            f"arn:aws:lambda:{region}:{account}:function:llmops-resurrector",
        ]}]}
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
    # Read-only billing work, so it defaults ON; --no-finops-schedule turns it off.
    ap.add_argument("--no-finops-schedule", action="store_true",
                    help="create the daily cost reconciliation schedule DISABLED")
    # Same posture as the finops schedule: $0 to run, and its value is that nobody has to
    # remember it. --no-sweep-schedule turns it off.
    ap.add_argument("--no-sweep-schedule", action="store_true",
                    help="create the daily orphan-endpoint sweep schedule DISABLED")
    # The dead-driver check costs one table Scan per 15 min and is the only thing that
    # reads the driver's heartbeat; --no-resurrector-schedule turns it off.
    ap.add_argument("--no-resurrector-schedule", action="store_true",
                    help="create the 15-minute dead-driver resurrector schedule DISABLED")
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
    finops_arn = f"arn:aws:lambda:{region}:{account}:function:llmops-finops-reconcile"
    results.append(ensure_finops_schedule(sched, finops_arn, role_arn,
                                          not args.no_finops_schedule, args.dry_run))
    sweep_arn = f"arn:aws:lambda:{region}:{account}:function:llmops-monitor-sweep"
    results.append(ensure_sweep_schedule(sched, sweep_arn, role_arn,
                                         not args.no_sweep_schedule, args.dry_run))
    res_arn = f"arn:aws:lambda:{region}:{account}:function:llmops-resurrector"
    results.append(ensure_resurrector_schedule(sched, res_arn, role_arn,
                                               not args.no_resurrector_schedule,
                                               args.dry_run))
    results.append(ensure_secret(sm, args.dry_run))
    api_res = ensure_api(apigw, lam, region, account, args.dry_run)
    results.append(api_res)
    if not args.dry_run and api_res.get("endpoint") not in (None, "?"):
        ssm.put_parameter(Name="/llmops/triggers/api_endpoint", Value=api_res["endpoint"],
                          Type="String", Overwrite=True)
    print(json.dumps({"results": results, "dry_run": args.dry_run}, indent=2, default=str))


if __name__ == "__main__":
    sys.exit(main())
