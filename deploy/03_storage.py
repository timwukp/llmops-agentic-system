#!/usr/bin/env python3
"""03_storage.py — provision state/artifact storage for llmops-agentic-system (idempotent).

Creates (all tagged project=llmops-agentic-system):
  - S3 data bucket (default llmops-agentic-<account_id>-<region>):
      versioning ON, SSE-S3 (AES256), public access block ALL,
      lifecycle: objects under runs/ expire after 90 days
  - DynamoDB llmops-pipeline-runs  (PK run_id S; GSI job_name-index on job_name S;
      on-demand; point-in-time recovery ON)
  - DynamoDB llmops-stage-events   (PK run_id S, SK sk S; on-demand)
  - DynamoDB llmops-cost-estimates (PK id S; GSI project-created_at-index; PITR on)
  - DynamoDB llmops-cost-actuals   (PK project S, SK sk S; PITR on)
  - EventBridge custom bus llmops-pipeline
  - SNS topic llmops-escalations
  - SSM parameters /llmops/storage/{bucket,runs_table,events_table,event_bus,escalations_topic_arn}

--dry-run prints the would-create plan without any AWS write. STS is only needed to
derive the default bucket name / topic check; pass --account-id for a fully offline dry-run.

Usage:
  python deploy/03_storage.py --region us-east-1 [--bucket NAME] [--dry-run]
  python deploy/03_storage.py --region us-east-1 --account-id 123456789012 --dry-run  # offline
"""
import argparse
import json
import pathlib
import sys

import boto3
from botocore.exceptions import ClientError

TAG_KEY, TAG_VAL = "project", "llmops-agentic-system"
RUNS_TABLE = "llmops-pipeline-runs"
EVENTS_TABLE = "llmops-stage-events"
#: Cost estimates and their approval decisions. Separate from the console's generic
#: table because an approval is an audit record: who asked, who approved, on what
#: number, at what time. PITR is on for the same reason.
ESTIMATES_TABLE = "llmops-cost-estimates"
#: Reconciled actuals, one row per (period, run, category), plus reserved #audit#/
#: #finding# rows from the finops agent. (PK, SK) mirrors llmops-stage-events so a
#: range query by period is natural.
ACTUALS_TABLE = "llmops-cost-actuals"
#: Tasks-tab consultations (goal → plan → signed acceptance → dispatch). PITR on:
#: the approval records inside are the "a human decided this" audit artifacts.
TASKS_TABLE = "llmops-tasks"
EVENT_BUS = "llmops-pipeline"
SNS_TOPIC = "llmops-escalations"
#: Asymmetric signing key for plan-acceptance records. The private half never leaves
#: KMS hardware, which is what makes an approval unforgeable after the fact.
APPROVAL_KEY_ALIAS = "alias/llmops-approval"
RUNS_EXPIRE_DAYS = 90


def safe_client(service, region, dry):
    """boto3 client, but degrade to None under --dry-run when the environment has
    no usable AWS config at all (fully offline dry-run with --account-id)."""
    try:
        return boto3.client(service, region_name=region)
    except Exception:
        if dry:
            return None
        raise


def ensure_bucket(s3, bucket, region, dry):
    exists = True
    try:
        s3.head_bucket(Bucket=bucket)
    except Exception:
        exists = False
    if dry:
        return "exists" if exists else "would create"
    if not exists:
        kwargs = {"Bucket": bucket}
        if region != "us-east-1":  # us-east-1 rejects a LocationConstraint
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
        s3.create_bucket(**kwargs)
        s3.get_waiter("bucket_exists").wait(Bucket=bucket)
    # The settings below are idempotent puts — safe to reapply on every run.
    s3.put_bucket_versioning(Bucket=bucket,
                             VersioningConfiguration={"Status": "Enabled"})
    s3.put_bucket_encryption(Bucket=bucket, ServerSideEncryptionConfiguration={
        "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
    s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True,
        "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={
        "Rules": [{
            "ID": f"expire-runs-{RUNS_EXPIRE_DAYS}d",
            "Status": "Enabled",
            "Filter": {"Prefix": "runs/"},
            "Expiration": {"Days": RUNS_EXPIRE_DAYS},
            "NoncurrentVersionExpiration": {"NoncurrentDays": RUNS_EXPIRE_DAYS},
        }]})
    s3.put_bucket_tagging(Bucket=bucket,
                          Tagging={"TagSet": [{"Key": TAG_KEY, "Value": TAG_VAL}]})
    return "exists" if exists else "created"


def ensure_contracts(s3, bucket, dry):
    """Upload pipeline/contracts/ to s3://<bucket>/contracts/.

    The finops harness is told to "read pipeline/contracts/cost_model.py and call it
    rather than recomputing costs in prose" -- a repo-relative path that means nothing
    inside an AgentCore container, which has no checkout. Proven live on the first
    pricing_refresh: the agent looked for the module on its filesystem, as an installed
    package, and in S3, found none of the three, and fell back to applying the merge
    precedence by hand -- stamping its own 37-SKU card `v1-DRAFT-noncanonical` because
    the `fallback_static` tier lives inside the module it could not reach.

    Uploading here rather than in a bespoke script keeps the module beside the buckets
    and tables the rest of the pipeline reads: one place that answers "what state does
    a fresh container start from". Lambdas keep vendoring the contracts into their zip
    (07_lambdas.py) -- a Lambda has no network guarantee to S3 at import time, whereas
    the harness does.
    """
    src = pathlib.Path(__file__).resolve().parent.parent / "pipeline" / "contracts"
    files = sorted(p for p in src.glob("*") if p.is_file())
    if dry:
        return {"would": f"upload {len(files)} contract files", "to": f"s3://{bucket}/contracts/"}
    for p in files:
        s3.upload_file(str(p), bucket, f"contracts/{p.name}")
        # Mirror under the repo-relative key too. The harness prompts cite the module
        # as pipeline/contracts/cost_model.py (its path in the repo), and a live
        # pricing_refresh showed the agent probing exactly that string as an S3 key
        # before giving up. Two keys, one upload path — cheaper than arguing with
        # every future prompt about which spelling is canonical.
        s3.upload_file(str(p), bucket, f"pipeline/contracts/{p.name}")
    return {"uploaded": [p.name for p in files],
            "to": [f"s3://{bucket}/contracts/", f"s3://{bucket}/pipeline/contracts/"]}


def ensure_table(ddb, name, spec, dry, pitr=False):
    try:
        ddb.describe_table(TableName=name)
        return "exists"
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
    except Exception:
        pass  # no credentials in offline dry-run
    if dry:
        return "would create"
    ddb.create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                     Tags=[{"Key": TAG_KEY, "Value": TAG_VAL}], **spec)
    ddb.get_waiter("table_exists").wait(TableName=name)
    if pitr:
        ddb.update_continuous_backups(
            TableName=name,
            PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True})
    return "created"


def ensure_approval_key(kms_client, account_id, dry):
    """ECC_NIST_P256 SIGN_VERIFY key + alias, idempotent by alias lookup.

    Key policy: root gets admin (losing key admin loses the audit trail's
    verifiability); Sign is granted via IAM identity policies (the console role's),
    which the default root-delegation policy permits; Verify/GetPublicKey likewise —
    verification is meant to be broadly available, that is the point of signing.
    """
    try:
        arn = kms_client.describe_key(KeyId=APPROVAL_KEY_ALIAS)["KeyMetadata"]["Arn"]
        return "exists", arn
    except Exception:
        pass
    if dry:
        return "would create", ""
    key = kms_client.create_key(
        Description="llmops plan-acceptance signing key (Tasks tab approvals)",
        KeyUsage="SIGN_VERIFY", KeySpec="ECC_NIST_P256",
        Tags=[{"TagKey": TAG_KEY, "TagValue": TAG_VAL}])
    arn = key["KeyMetadata"]["Arn"]
    kms_client.create_alias(AliasName=APPROVAL_KEY_ALIAS,
                            TargetKeyId=key["KeyMetadata"]["KeyId"])
    return "created", arn


def ensure_bus(events, dry):
    try:
        events.describe_event_bus(Name=EVENT_BUS)
        return "exists"
    except Exception:
        pass
    if dry:
        return "would create"
    events.create_event_bus(Name=EVENT_BUS,
                            Tags=[{"Key": TAG_KEY, "Value": TAG_VAL}])
    return "created"


def ensure_topic(sns, topic_arn, dry, email=None):
    """Create the escalation topic and make sure SOMEONE is listening.

    The topic existed from Phase 3 with zero subscribers, so every escalation the
    pipeline raised -- an agent stopping to ask a human about a budget overrun, a
    quality gate failing, a remediation budget exhausted -- published successfully
    into nothing. An escalation nobody receives is worse than none at all: the run
    waits and the design claims a human was asked.

    The deploy cannot invent an address, so: subscribe one when given (idempotent --
    SNS ignores a repeat of the same endpoint), and when nobody at all is subscribed,
    say so in the output rather than reporting the topic as healthy.
    """
    status = "exists"
    arn = topic_arn
    try:
        sns.get_topic_attributes(TopicArn=topic_arn)
    except Exception:
        if dry:
            return {"status": "would create", "arn": topic_arn}
        # create_topic is itself idempotent by name
        arn = sns.create_topic(Name=SNS_TOPIC,
                               Tags=[{"Key": TAG_KEY, "Value": TAG_VAL}])["TopicArn"]
        status = "created"

    if dry:
        return {"status": status, "arn": arn,
                "subscribers": f"would subscribe {email}" if email else "unchanged"}

    subs = sns.list_subscriptions_by_topic(TopicArn=arn).get("Subscriptions", [])
    existing = {s.get("Endpoint") for s in subs}
    if email and email not in existing:
        sns.subscribe(TopicArn=arn, Protocol="email", Endpoint=email,
                      ReturnSubscriptionArn=True)
        subs.append({"Endpoint": email, "SubscriptionArn": "pending confirmation"})

    note = f"{len(subs)} subscriber(s)"
    if not subs:
        note = ("NO SUBSCRIBERS -- every escalate_human call publishes into the void. "
                "Re-run with --escalation-email <addr> so a human actually hears them.")
    # A subscription stays PendingConfirmation until the recipient clicks the link;
    # reporting it as subscribed would be the same silence in a new place.
    pending = [s["Endpoint"] for s in subs
               if str(s.get("SubscriptionArn", "")).lower().find("pending") >= 0]
    if pending:
        note += f" (awaiting email confirmation: {', '.join(pending)})"
    return {"status": status, "arn": arn, "subscribers": note}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", required=True)
    ap.add_argument("--account-id", help="skip STS (offline dry-run) and use this account id")
    ap.add_argument("--bucket", help="bucket name (default llmops-agentic-<acct>-<region>)")
    ap.add_argument("--escalation-email",
                    help="subscribe this address to llmops-escalations (idempotent). "
                         "Without a subscriber, escalate_human publishes into the void.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    account_id = args.account_id
    if not account_id:
        account_id = boto3.client("sts", region_name=args.region).get_caller_identity()["Account"]
    bucket = args.bucket or f"llmops-agentic-{account_id}-{args.region}"
    topic_arn = f"arn:aws:sns:{args.region}:{account_id}:{SNS_TOPIC}"

    s3 = safe_client("s3", args.region, args.dry_run)
    ddb = safe_client("dynamodb", args.region, args.dry_run)
    events = safe_client("events", args.region, args.dry_run)
    sns = safe_client("sns", args.region, args.dry_run)

    results = {}
    results["bucket"] = {
        "name": bucket,
        "status": ensure_bucket(s3, bucket, args.region, args.dry_run),
        "settings": "versioning=on sse=AES256 public-access-block=ALL "
                    f"lifecycle=runs/ expire {RUNS_EXPIRE_DAYS}d",
    }
    results["contracts"] = ensure_contracts(s3, bucket, args.dry_run)
    results[RUNS_TABLE] = {
        "status": ensure_table(ddb, RUNS_TABLE, {
            "AttributeDefinitions": [
                {"AttributeName": "run_id", "AttributeType": "S"},
                {"AttributeName": "job_name", "AttributeType": "S"},
            ],
            "KeySchema": [{"AttributeName": "run_id", "KeyType": "HASH"}],
            "GlobalSecondaryIndexes": [{
                "IndexName": "job_name-index",
                "KeySchema": [{"AttributeName": "job_name", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
        }, args.dry_run, pitr=True),
        "schema": "PK run_id (S), GSI job_name-index, on-demand, PITR on",
    }
    results[EVENTS_TABLE] = {
        "status": ensure_table(ddb, EVENTS_TABLE, {
            "AttributeDefinitions": [
                {"AttributeName": "run_id", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            "KeySchema": [
                {"AttributeName": "run_id", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
        }, args.dry_run),
        "schema": "PK run_id (S), SK sk (S), on-demand",
    }
    results[ESTIMATES_TABLE] = {
        "status": ensure_table(ddb, ESTIMATES_TABLE, {
            "AttributeDefinitions": [
                {"AttributeName": "id", "AttributeType": "S"},
                {"AttributeName": "project", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
            ],
            "KeySchema": [{"AttributeName": "id", "KeyType": "HASH"}],
            # The approval queue and the project rollup both read "this project's
            # estimates, newest first". Without this GSI both would be table scans,
            # and the queue is on the dashboard's default render path.
            "GlobalSecondaryIndexes": [{
                "IndexName": "project-created_at-index",
                "KeySchema": [
                    {"AttributeName": "project", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
        }, args.dry_run, pitr=True),
        "schema": "PK id (S), GSI project-created_at-index, on-demand, PITR on "
                  "(approval decisions are audit records)",
    }
    results[ACTUALS_TABLE] = {
        "status": ensure_table(ddb, ACTUALS_TABLE, {
            "AttributeDefinitions": [
                {"AttributeName": "project", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            "KeySchema": [
                {"AttributeName": "project", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
        }, args.dry_run, pitr=True),
        "schema": "PK project (S), SK sk = <period>#<run_id|audit|finding>#<...>, "
                  "on-demand, PITR on",
    }
    results[TASKS_TABLE] = {
        "status": ensure_table(ddb, TASKS_TABLE, {
            "AttributeDefinitions": [
                {"AttributeName": "id", "AttributeType": "S"},
            ],
            "KeySchema": [{"AttributeName": "id", "KeyType": "HASH"}],
        }, args.dry_run, pitr=True),
        "schema": "PK id (S, task- prefix), on-demand, PITR on; list via scan "
                  "(deliberate simplification — consultation volumes are tiny)",
    }
    results["event_bus"] = {"name": EVENT_BUS, "status": ensure_bus(events, args.dry_run)}
    topic = ensure_topic(sns, topic_arn, args.dry_run, args.escalation_email)
    topic_arn = topic["arn"]
    results["sns_topic"] = {"name": SNS_TOPIC, "status": topic["status"],
                            "subscribers": topic.get("subscribers", "")}
    kms_client = safe_client("kms", args.region, args.dry_run)
    key_status, key_arn = ensure_approval_key(kms_client, account_id, args.dry_run)
    results["approval_key"] = {"alias": APPROVAL_KEY_ALIAS, "status": key_status,
                               "arn": key_arn}

    params = {
        "bucket": bucket,
        "runs_table": RUNS_TABLE,
        "events_table": EVENTS_TABLE,
        "estimates_table": ESTIMATES_TABLE,
        "actuals_table": ACTUALS_TABLE,
        "tasks_table": TASKS_TABLE,
        "event_bus": EVENT_BUS,
        "escalations_topic_arn": topic_arn,
    }
    if key_arn:
        params["approval_key_arn"] = key_arn
    if not args.dry_run:
        ssm = boto3.client("ssm", region_name=args.region)
        for k, v in params.items():
            ssm.put_parameter(Name=f"/llmops/storage/{k}", Value=v,
                              Type="String", Overwrite=True)
    results["ssm_params"] = [f"/llmops/storage/{k}" for k in params]
    results["dry_run"] = args.dry_run

    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
