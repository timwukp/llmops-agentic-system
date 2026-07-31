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
EVENT_BUS = "llmops-pipeline"
SNS_TOPIC = "llmops-escalations"
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


def ensure_topic(sns, topic_arn, dry):
    try:
        sns.get_topic_attributes(TopicArn=topic_arn)
        return "exists", topic_arn
    except Exception:
        pass
    if dry:
        return "would create", topic_arn
    # create_topic is itself idempotent by name
    arn = sns.create_topic(Name=SNS_TOPIC,
                           Tags=[{"Key": TAG_KEY, "Value": TAG_VAL}])["TopicArn"]
    return "created", arn


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", required=True)
    ap.add_argument("--account-id", help="skip STS (offline dry-run) and use this account id")
    ap.add_argument("--bucket", help="bucket name (default llmops-agentic-<acct>-<region>)")
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
    results["event_bus"] = {"name": EVENT_BUS, "status": ensure_bus(events, args.dry_run)}
    status, topic_arn = ensure_topic(sns, topic_arn, args.dry_run)
    results["sns_topic"] = {"name": SNS_TOPIC, "status": status}

    params = {
        "bucket": bucket,
        "runs_table": RUNS_TABLE,
        "events_table": EVENTS_TABLE,
        "estimates_table": ESTIMATES_TABLE,
        "actuals_table": ACTUALS_TABLE,
        "event_bus": EVENT_BUS,
        "escalations_topic_arn": topic_arn,
    }
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
