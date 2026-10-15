#!/usr/bin/env python3
"""10_lakehouse.py — the lakehouse storage Phase 1 is built on (idempotent). Phase 1 PR-04.

The design is docs/research/LAKEHOUSE_LLM_LIFECYCLE.md §4.3; the substrate it relies on was
proved live by spike S7 (deploy/evidence/SPIKE_07_s7_lakehouse_substrate.md): a table created
through the S3 Tables API is Iceberg v2, a Glue 5 job writes it with IAM-only grants through the
`s3tablescatalog` federation, and `UNLOAD … FOR VERSION AS OF` exports a snapshot
deterministically.

Creates (buckets and keys tagged project=llmops-agentic-system):
  - KMS keys alias/llmops-lakehouse-raw and alias/llmops-lakehouse-curated, rotation on. One
      key per bucket on purpose: Bronze holds PII, so being able to decrypt the raw key IS
      access to the PII, and that should not come bundled with the de-identified tables. Each
      key policy grants the account (so IAM policies govern use) and the S3 Tables maintenance
      principal, scoped by encryption context to its own bucket -- without the latter,
      CreateTable on an SSE-KMS bucket fails outright.
  - Table buckets llmops-raw (namespace bronze) and llmops-curated (namespaces silver, gold),
      SSE-KMS with their keys.
  - Tables (TABLES below): Iceberg, format-version 2, lowercase names, schemas and partition
      specs declared here. No table tags (§4.3).
  - Retention: Bronze snapshots expire after 72 h and the raw bucket removes unreferenced and
      noncurrent files after 1 day, so a right-to-be-forgotten DELETE becomes a physical delete
      within days. Curated keeps the S3 Tables defaults: training reproducibility rests on the
      UNLOAD extract and its sha256 (§4.4), not on keeping snapshots alive.
  - Athena workgroup llmops-lakehouse (engine v3, results under s3://<DATA_BUCKET>/athena/lakehouse/,
      configuration enforced, SSE-S3).
  - IAM role llmops-lakehouse-glue for the Bronze→Silver→Gold jobs (PR-07): S3 Tables data on
      the two buckets, the federated catalog entries for them, the two keys, its own script and
      temp prefix, its own log groups. No Lake Formation: S7 showed IAM-only grants suffice.
  - SSM /llmops/lakehouse/{raw_bucket_arn,curated_bucket_arn,raw_kms_key_arn,
      curated_kms_key_arn,workgroup,glue_role_arn}

Requires the account-level S3 Tables ↔ Glue Data Catalog integration (`s3tablescatalog`); it
exists (PROJECT_STATE.md). This script REPORTS its absence and stops; it never enables it.

STANDING COST: two customer-managed KMS keys ($1/month each) plus S3 Tables storage and
maintenance for whatever data lands (empty tables cost nothing). Teardown:
  python deploy/10_lakehouse.py --region <r> --teardown
which deletes the tables (and their data), namespaces, buckets, workgroup and role, and SCHEDULES
the keys for deletion (30-day window; cancel with kms cancel-key-deletion).

Usage:
  python deploy/10_lakehouse.py --region us-east-1 [--dry-run] [--account-id <id>]
  python deploy/10_lakehouse.py --region us-east-1 --verify
  python deploy/10_lakehouse.py --region us-east-1 --teardown [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time

PROJECT_TAG = {"project": "llmops-agentic-system"}
RAW, CURATED = "llmops-raw", "llmops-curated"
KEY_ALIAS = {RAW: "alias/llmops-lakehouse-raw", CURATED: "alias/llmops-lakehouse-curated"}
NAMESPACES = {RAW: ("bronze",), CURATED: ("silver", "gold")}
WORKGROUP = "llmops-lakehouse"
GLUE_ROLE = "llmops-lakehouse-glue"
#: PR-09: the generator writes landing, which the transforms' role is denied, so it runs as a role
#: of its own. Neither job can then rewrite the other's input.
GENERATOR_ROLE = "llmops-lakehouse-generator"
GLUE_PREFIX = "lakehouse/glue"
LANDING_PREFIX = "lakehouse/landing"
REPORT_PREFIX = "lakehouse/reports"
ATHENA_PREFIX = "athena/lakehouse"
#: PR-07: the guardrail that finds NAMES for the Bronze→Silver job (spike S4: structured identifiers
#: by checksum regex, names by Guardrails). Both directions enabled explicitly -- with only
#: `action` set, ApplyGuardrail(source="INPUT") detected nothing (S4 run 1).
GUARDRAIL_NAME = "llmops-lakehouse-pii"
JOBS = {"generate": ("llmops-lakehouse-generate", "generate_landing.py"),
        "land": ("llmops-lakehouse-land-bronze", "land_bronze.py"),
        "silver": ("llmops-lakehouse-bronze-to-silver", "bronze_to_silver.py"),
        "gold": ("llmops-lakehouse-silver-to-gold", "silver_to_gold.py"),
        "export": ("llmops-lakehouse-export", "export_gold.py")}
#: PR-08: Gold extracts live under their own top-level prefix so one Macie job can cover them
#: (Macie scopes by key prefix only).
EXTRACTS_PREFIX = "lakehouse/extracts"
#: The export only drives Athena and S3, and the generator is stdlib Python: Python-shell jobs at
#: 1/16 DPU, not Spark clusters. Each ships the one module it imports.
PYTHON_SHELL_JOBS = {"export_gold.py", "generate_landing.py"}
SHELL_JOB_MODULE = {"export_gold.py": "export.py", "generate_landing.py": "generate.py"}
REPO = __import__("pathlib").Path(__file__).resolve().parents[1]
GLUE_SRC = REPO / "pipeline" / "lakehouse" / "glue"
TRANSFORM_SRC = REPO / "pipeline" / "lakehouse" / "transform.py"
EXPORT_SRC = REPO / "pipeline" / "lakehouse" / "export.py"
GENERATE_SRC = REPO / "pipeline" / "lakehouse" / "generate.py"
ORG_ENTITIES_SRC = REPO / "pipeline" / "lakehouse" / "reference" / "org_entities.json"
SSM_PREFIX = "/llmops/lakehouse"
KEY_DELETION_DAYS = 30

#: Bronze holds PII: short-lived snapshots and fast physical removal of unreferenced files.
RAW_SNAPSHOT = {"minSnapshotsToKeep": 1, "maxSnapshotAgeHours": 72}
RAW_FILE_REMOVAL = {"unreferencedDays": 1, "nonCurrentDays": 1}

#: Iceberg primitive types accepted in a table's simple schema.
ICEBERG_TYPES = {"boolean", "int", "long", "float", "double", "date", "time", "timestamp",
                 "timestamptz", "string", "uuid", "binary"}
PARTITION_TRANSFORMS = {"identity", "year", "month", "day", "hour"}


def _t(bucket, namespace, name, fields, partition=()):
    return {"bucket": bucket, "namespace": namespace, "name": name,
            "fields": [{"id": i + 1, "name": n, "type": ty, "required": req}
                       for i, (n, ty, req) in enumerate(fields)],
            "partition": list(partition)}


#: §2 and §4.3 of the research report. (name, type, required); partition = (source, transform, name).
TABLES = [
    _t(RAW, "bronze", "conversation_events", [
        ("event_id", "string", True), ("conversation_id", "string", True),
        ("turn_idx", "int", True), ("role", "string", True), ("content", "string", False),
        ("lang", "string", False), ("raw_meta", "string", False),
        ("ingest_ts", "timestamptz", True)], [("ingest_ts", "day", "ingest_day")]),
    _t(RAW, "bronze", "crm_cdc", [
        ("op", "string", True), ("before", "string", False), ("after", "string", False),
        ("ts_ms", "long", True), ("source", "string", False),
        ("ingest_ts", "timestamptz", True)], [("ingest_ts", "day", "ingest_day")]),
    _t(RAW, "bronze", "erp_cdc", [
        ("op", "string", True), ("before", "string", False), ("after", "string", False),
        ("ts_ms", "long", True), ("source", "string", False),
        ("ingest_ts", "timestamptz", True)], [("ingest_ts", "day", "ingest_day")]),
    # §2 lists web/business-system logs as a source landing in bronze.web_events; §4.3's table
    # list omits it, which is the report's gap, not a decision.
    _t(RAW, "bronze", "web_events", [
        ("event_id", "string", True), ("session_id", "string", False),
        ("customer_id", "string", False), ("event_type", "string", True),
        ("page", "string", False), ("payload", "string", False),
        ("event_ts", "timestamptz", True)], [("event_ts", "day", "event_day")]),
    _t(RAW, "bronze", "inference_logs", [
        ("request_id", "string", True), ("adapter_ic", "string", False),
        ("model_version", "string", False), ("prompt_redacted", "string", False),
        ("response_redacted", "string", False), ("input_tokens", "int", False),
        ("output_tokens", "int", False), ("latency_ms", "int", False),
        ("event_ts", "timestamptz", True)], [("event_ts", "day", "event_day")]),
    _t(RAW, "bronze", "feedback", [
        ("request_id", "string", True), ("rating", "int", False),
        ("comment_redacted", "string", False), ("event_ts", "timestamptz", True)],
       [("event_ts", "day", "event_day")]),
    _t(CURATED, "silver", "instruction_pairs", [
        ("pair_id", "string", True), ("instruction", "string", True), ("input", "string", False),
        ("output", "string", True), ("lang", "string", True), ("source_ref", "string", True),
        ("pii_status", "string", True), ("redaction_method", "string", False),
        ("dq_run_id", "string", False), ("updated_ts", "timestamptz", True)],
       [("lang", "identity", "lang")]),
    _t(CURATED, "gold", "qlora_train", [
        ("pair_id", "string", True), ("instruction", "string", True), ("input", "string", False),
        ("output", "string", True), ("split", "string", True)]),
    _t(CURATED, "gold", "qlora_eval", [
        ("pair_id", "string", True), ("instruction", "string", True), ("input", "string", False),
        ("output", "string", True), ("split", "string", True)]),
    _t(CURATED, "gold", "dataset_manifest", [
        ("dataset_version", "string", True), ("table_name", "string", True),
        ("snapshot_id", "long", True), ("export_uri", "string", True),
        ("sha256", "string", True), ("row_count", "long", True), ("dq_score", "double", False),
        ("pii_report_uri", "string", False), ("run_id", "string", False),
        ("created_ts", "timestamptz", True)]),
]

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


# ── pure: specs, policies, plan ──────────────────────────────────────────────
def validate_tables(tables=TABLES) -> None:
    """Raise on anything Glue/Athena/S3 Tables would reject or silently hide."""
    seen = set()
    for t in tables:
        key = (t["bucket"], t["namespace"], t["name"])
        if key in seen:
            raise ValueError(f"duplicate table {key}")
        seen.add(key)
        if t["namespace"] not in NAMESPACES[t["bucket"]]:
            raise ValueError(f"{t['name']}: namespace {t['namespace']} is not in {t['bucket']}")
        for n in (t["namespace"], t["name"], *(f["name"] for f in t["fields"])):
            # Uppercase names are invisible to Glue/Lake Formation/Athena (§4.3).
            if not _NAME.match(n):
                raise ValueError(f"{t['name']}: {n!r} is not a lowercase identifier")
        names = [f["name"] for f in t["fields"]]
        if len(set(names)) != len(names) or len({f["id"] for f in t["fields"]}) != len(names):
            raise ValueError(f"{t['name']}: duplicate field name or id")
        for f in t["fields"]:
            if f["type"] not in ICEBERG_TYPES:
                raise ValueError(f"{t['name']}.{f['name']}: unknown Iceberg type {f['type']}")
        for src, transform, _ in t["partition"]:
            if src not in names:
                raise ValueError(f"{t['name']}: partition source {src} is not a column")
            if transform not in PARTITION_TRANSFORMS:
                raise ValueError(f"{t['name']}: unsupported transform {transform}")


def table_metadata(t: dict) -> dict:
    """CreateTable `metadata` for one spec: the simple schema plus the partition spec."""
    ids = {f["name"]: f["id"] for f in t["fields"]}
    iceberg = {"schema": {"fields": [dict(f) for f in t["fields"]]}}
    if t["partition"]:
        iceberg["partitionSpec"] = {"specId": 0, "fields": [
            {"sourceId": ids[src], "transform": transform, "name": name,
             "fieldId": 1000 + i} for i, (src, transform, name) in enumerate(t["partition"])]}
    return {"iceberg": iceberg}


def bucket_arn(region: str, account_id: str, name: str) -> str:
    return f"arn:aws:s3tables:{region}:{account_id}:bucket/{name}"


def key_policy(region: str, account_id: str, bucket: str) -> dict:
    """The account administers the key (so IAM policies decide who uses it), and the S3 Tables
    maintenance principal may use it for THIS bucket only. No other principal is named here:
    callers get the key through their IAM policies, which is where least privilege is visible."""
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "AccountAdministersTheKey", "Effect": "Allow",
         "Principal": {"AWS": f"arn:aws:iam::{account_id}:root"},
         "Action": "kms:*", "Resource": "*"},
        {"Sid": "S3TablesMaintenanceForThisBucket", "Effect": "Allow",
         "Principal": {"Service": "maintenance.s3tables.amazonaws.com"},
         "Action": ["kms:GenerateDataKey", "kms:Decrypt"], "Resource": "*",
         "Condition": {"StringLike": {
             "kms:EncryptionContext:aws:s3:arn": f"{bucket_arn(region, account_id, bucket)}/*"}}},
    ]}


def glue_trust_policy(account_id: str) -> dict:
    return {"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
            "Principal": {"Service": "glue.amazonaws.com"}, "Action": "sts:AssumeRole",
            "Condition": {"StringEquals": {"aws:SourceAccount": account_id}}}]}


def glue_role_policy(region: str, account_id: str, data_bucket: str, key_arns: dict,
                     guardrail_arn: str | None = None) -> dict:
    """The S7-proven grants, widened from one spike bucket to the two lakehouse buckets, plus
    what the PR-07 jobs need: landing read, report write, the one guardrail, DQ publishing."""
    cat = f"arn:aws:glue:{region}:{account_id}"
    buckets = [bucket_arn(region, account_id, b) for b in (RAW, CURATED)]
    catalog = [f"{cat}:catalog", f"{cat}:catalog/s3tablescatalog"]
    for b in (RAW, CURATED):
        catalog.append(f"{cat}:catalog/s3tablescatalog/{b}")
        for ns in NAMESPACES[b]:
            catalog += [f"{cat}:database/s3tablescatalog/{b}/{ns}",
                        f"{cat}:table/s3tablescatalog/{b}/{ns}/*"]
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "S3TablesData", "Effect": "Allow",
         "Action": ["s3tables:GetTableBucket", "s3tables:GetNamespace", "s3tables:GetTable",
                    "s3tables:GetTableMetadataLocation", "s3tables:UpdateTableMetadataLocation",
                    "s3tables:GetTableData", "s3tables:PutTableData"],
         "Resource": buckets + [f"{b}/table/*" for b in buckets]},
        {"Sid": "GlueCatalogFederation", "Effect": "Allow",
         "Action": ["glue:GetCatalog", "glue:GetDatabase", "glue:GetTable", "glue:UpdateTable"],
         "Resource": catalog},
        {"Sid": "LakehouseKeys", "Effect": "Allow", "Action": ["kms:GenerateDataKey", "kms:Decrypt"],
         "Resource": [key_arns[RAW], key_arns[CURATED]]},
        {"Sid": "ScriptsAndTemp", "Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"],
         "Resource": f"arn:aws:s3:::{data_bucket}/{GLUE_PREFIX}/*"},
        {"Sid": "OwnLogs", "Effect": "Allow",
         "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{region}:{account_id}:log-group:/aws-glue/*"},
        {"Sid": "LandingRead", "Effect": "Allow", "Action": "s3:GetObject",
         "Resource": f"arn:aws:s3:::{data_bucket}/{LANDING_PREFIX}/*"},
        {"Sid": "ListOwnPrefixes", "Effect": "Allow", "Action": "s3:ListBucket",
         "Resource": f"arn:aws:s3:::{data_bucket}",
         "Condition": {"StringLike": {"s3:prefix": [f"{p}/*" for p in (
             GLUE_PREFIX, LANDING_PREFIX, REPORT_PREFIX, EXTRACTS_PREFIX, ATHENA_PREFIX)]}}},
        {"Sid": "ExtractsReadWrite", "Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"],
         "Resource": [f"arn:aws:s3:::{data_bucket}/{EXTRACTS_PREFIX}/*",
                      f"arn:aws:s3:::{data_bucket}/{ATHENA_PREFIX}/*"]},
        {"Sid": "ResultBucketLocation", "Effect": "Allow", "Action": "s3:GetBucketLocation",
         "Resource": f"arn:aws:s3:::{data_bucket}"},
        {"Sid": "ExportQueries", "Effect": "Allow",
         "Action": ["athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults"],
         "Resource": f"arn:aws:athena:{region}:{account_id}:workgroup/{WORKGROUP}"},
        {"Sid": "ReportsRead", "Effect": "Allow", "Action": "s3:GetObject",
         "Resource": f"arn:aws:s3:::{data_bucket}/{REPORT_PREFIX}/*"},
        {"Sid": "ReportsWrite", "Effect": "Allow", "Action": "s3:PutObject",
         "Resource": f"arn:aws:s3:::{data_bucket}/{REPORT_PREFIX}/*"},
        {"Sid": "PublishDataQuality", "Effect": "Allow", "Action": "glue:PublishDataQuality",
         "Resource": f"arn:aws:glue:{region}:{account_id}:dataQualityRuleset/*"},
    ] + ([{"Sid": "NamesByGuardrail", "Effect": "Allow", "Action": "bedrock:ApplyGuardrail",
           "Resource": guardrail_arn}] if guardrail_arn else [])}


def generator_role_policy(region: str, account_id: str, data_bucket: str) -> dict:
    """Write landing, read its own script and module, write its logs. No table, no key, no
    report: it produces source files and nothing downstream of them."""
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "ScriptsRead", "Effect": "Allow", "Action": "s3:GetObject",
         "Resource": f"arn:aws:s3:::{data_bucket}/{GLUE_PREFIX}/*"},
        {"Sid": "LandingWrite", "Effect": "Allow", "Action": "s3:PutObject",
         "Resource": f"arn:aws:s3:::{data_bucket}/{LANDING_PREFIX}/*"},
        {"Sid": "LandingList", "Effect": "Allow", "Action": "s3:ListBucket",
         "Resource": f"arn:aws:s3:::{data_bucket}",
         "Condition": {"StringLike": {"s3:prefix": [f"{LANDING_PREFIX}/*"]}}},
        {"Sid": "OwnLogs", "Effect": "Allow",
         "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{region}:{account_id}:log-group:/aws-glue/*"},
    ]}


def workgroup_config(data_bucket: str) -> dict:
    """No EncryptionConfiguration, on purpose. With one set (SSE_S3, or SSE_KMS even naming the
    table's own key), every Athena INSERT into a CMK-encrypted S3 Table fails with
    INTERNAL_ERROR_QUERY_ENGINE, while the same INSERT into an SSE-S3 table succeeds (measured
    2026-10-15, PR-08). Results and UNLOAD extracts still land encrypted: the data bucket's default
    encryption applies, and verify() checks that it is there."""
    return {"ResultConfiguration": {"OutputLocation": f"s3://{data_bucket}/{ATHENA_PREFIX}/"},
            "EnforceWorkGroupConfiguration": True,
            "EngineVersion": {"SelectedEngineVersion": "Athena engine version 3"},
            "PublishCloudWatchMetricsEnabled": True}


def plan(region: str, account_id: str, data_bucket: str) -> list[dict]:
    items = []
    for b in (RAW, CURATED):
        items.append({"kind": "kms key", "name": f"{KEY_ALIAS[b]} (rotation on)"})
        items.append({"kind": "table bucket", "name": f"{b} (SSE-KMS {KEY_ALIAS[b]})"})
        items += [{"kind": "namespace", "name": f"{b}/{ns}"} for ns in NAMESPACES[b]]
    items += [{"kind": "table", "name": f"{t['bucket']}/{t['namespace']}.{t['name']} "
                                        f"({len(t['fields'])} columns"
                                        + (f", partitioned by {', '.join(p[2] for p in t['partition'])}"
                                           if t["partition"] else "") + ")"} for t in TABLES]
    items.append({"kind": "maintenance", "name": f"{RAW}: snapshots {RAW_SNAPSHOT}, files {RAW_FILE_REMOVAL}"})
    items.append({"kind": "athena workgroup", "name": f"{WORKGROUP} → s3://{data_bucket}/{ATHENA_PREFIX}/"})
    items.append({"kind": "guardrail", "name": f"{GUARDRAIL_NAME} (NAME, ANONYMIZE both directions, numbered version)"})
    items.append({"kind": "iam role", "name": f"arn:aws:iam::{account_id}:role/{GLUE_ROLE}"})
    items += [{"kind": "glue job", "name": f"{n} (Glue 5.0, {sc})"} for n, sc in JOBS.values()]
    items += [{"kind": "ssm parameter", "name": f"{SSM_PREFIX}/{k}"} for k in SSM_KEYS]
    return items


SSM_KEYS = ("raw_bucket_arn", "curated_bucket_arn", "raw_kms_key_arn", "curated_kms_key_arn",
            "workgroup", "glue_role_arn", "guardrail_id", "guardrail_version")


# ── AWS operations ───────────────────────────────────────────────────────────
def _tags_list(extra=None):
    return [{"Key": k, "Value": v} for k, v in {**PROJECT_TAG, **(extra or {})}.items()]


def ensure_key(kms, region, account_id, bucket) -> tuple[str, str]:
    alias = KEY_ALIAS[bucket]
    for page in kms.get_paginator("list_aliases").paginate():
        for a in page["Aliases"]:
            if a["AliasName"] == alias and a.get("TargetKeyId"):
                return kms.describe_key(KeyId=a["TargetKeyId"])["KeyMetadata"]["Arn"], "exists"
    key = kms.create_key(Policy=json.dumps(key_policy(region, account_id, bucket)),
                         Description=f"llmops lakehouse: table bucket {bucket}",
                         KeyUsage="ENCRYPT_DECRYPT", KeySpec="SYMMETRIC_DEFAULT",
                         Tags=[{"TagKey": k, "TagValue": v} for k, v in PROJECT_TAG.items()])
    arn = key["KeyMetadata"]["Arn"]
    kms.enable_key_rotation(KeyId=arn)
    kms.create_alias(AliasName=alias, TargetKeyId=arn)
    return arn, "created"


def find_bucket(s3t, name) -> str | None:
    for b in s3t.list_table_buckets(prefix=name).get("tableBuckets", []):
        if b["name"] == name:
            return b["arn"]
    return None


def ensure_bucket(s3t, name, key_arn) -> tuple[str, str]:
    arn = find_bucket(s3t, name)
    if arn:
        return arn, "exists"
    arn = s3t.create_table_bucket(name=name, tags=dict(PROJECT_TAG), encryptionConfiguration={
        "sseAlgorithm": "aws:kms", "kmsKeyArn": key_arn})["arn"]
    return arn, "created"


def ensure_namespace(s3t, arn, ns) -> str:
    try:
        s3t.get_namespace(tableBucketARN=arn, namespace=ns)
        return "exists"
    except s3t.exceptions.NotFoundException:
        s3t.create_namespace(tableBucketARN=arn, namespace=[ns])
        return "created"


def ensure_table(s3t, arn, t) -> str:
    try:
        s3t.get_table(tableBucketARN=arn, namespace=t["namespace"], name=t["name"])
        return "exists"
    except s3t.exceptions.NotFoundException:
        s3t.create_table(tableBucketARN=arn, namespace=t["namespace"], name=t["name"],
                         format="ICEBERG", metadata=table_metadata(t))
        return "created"


def ensure_generator_role(iam, region, account_id, data_bucket) -> str:
    try:
        arn = iam.get_role(RoleName=GENERATOR_ROLE)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=GENERATOR_ROLE, AssumeRolePolicyDocument=json.dumps(
            glue_trust_policy(account_id)), Tags=_tags_list(),
            Description="llmops lakehouse source generator (writes landing only)")["Role"]["Arn"]
    iam.put_role_policy(RoleName=GENERATOR_ROLE, PolicyName="lakehouse-generator",
                        PolicyDocument=json.dumps(generator_role_policy(region, account_id, data_bucket)))
    return arn


def ensure_role(iam, region, account_id, data_bucket, key_arns, guardrail_arn=None) -> str:
    try:
        arn = iam.get_role(RoleName=GLUE_ROLE)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=GLUE_ROLE, AssumeRolePolicyDocument=json.dumps(
            glue_trust_policy(account_id)), Tags=_tags_list(),
            Description="llmops lakehouse Glue jobs (Bronze->Silver->Gold)")["Role"]["Arn"]
    iam.put_role_policy(RoleName=GLUE_ROLE, PolicyName="lakehouse", PolicyDocument=json.dumps(
        glue_role_policy(region, account_id, data_bucket, key_arns, guardrail_arn)))
    return arn


def guardrail_config() -> dict:
    return {"piiEntitiesConfig": [{"type": "NAME", "action": "ANONYMIZE", "inputAction": "ANONYMIZE",
                                   "outputAction": "ANONYMIZE", "inputEnabled": True, "outputEnabled": True}]}


def ensure_guardrail(bedrock) -> tuple[str, str, str, str]:
    """(id, arn, version, status). A numbered version, never DRAFT: the job's detections must
    come from a fixed configuration, or two runs over the same data are not comparable."""
    found = next((g for page in [bedrock.list_guardrails(maxResults=100)] for g in page.get("guardrails", [])
                  if g["name"] == GUARDRAIL_NAME), None)
    if found is None:
        g = bedrock.create_guardrail(
            name=GUARDRAIL_NAME, description="llmops lakehouse: names in customer-service text",
            sensitiveInformationPolicyConfig=guardrail_config(),
            blockedInputMessaging="blocked", blockedOutputsMessaging="blocked",
            tags=[{"key": k, "value": v} for k, v in PROJECT_TAG.items()])
        gid, arn, status = g["guardrailId"], g["guardrailArn"], "created"
    else:
        gid, arn, status = found["id"], found["arn"], "exists"
    versions = [v["version"] for v in bedrock.list_guardrails(guardrailIdentifier=gid).get("guardrails", [])
                if v.get("version") not in (None, "DRAFT")]
    version = max(versions, key=int) if versions else bedrock.create_guardrail_version(
        guardrailIdentifier=gid, description="PR-07")["version"]
    return gid, arn, version, status


#: Glue 5 bundles a boto3 without bedrock-runtime apply_guardrail (live, smoke run 1:
#: AttributeError). The Bronze→Silver job installs the repo's SDK floor instead (AGENTS.md).
JOB_PYTHON_MODULES = {"bronze_to_silver.py": "boto3==1.43.57,botocore==1.43.57"}


def run_arguments(step: str, data_bucket: str, dataset: str, **kw) -> dict:
    """The per-run arguments of each job: the one place the lakehouse's S3 layout is spelled out
    for a run. The smoke passes exactly this, and the state machine builds the same strings with
    States.Format (tests/test_lakehouse_mode.py renders the ASL and compares)."""
    landing = f"s3://{data_bucket}/{LANDING_PREFIX}/{dataset}/"
    reports = f"s3://{data_bucket}/{REPORT_PREFIX}/{dataset}/"
    return {
        "generate": lambda: {"--dataset": dataset, "--seed": str(kw["seed"]), "--scale": kw["scale"]},
        "land": lambda: {"--landing_uri": landing, "--report_uri": reports + "land.json"},
        "silver": lambda: {"--eval_prompts_uri": landing + "eval_prompts.jsonl",
                           "--report_uri": reports + "silver.json", "--dq_run_id": dataset,
                           "--expected_defects_uri": landing + "expected_defects.json"},
        "gold": lambda: {"--report_uri": reports + "gold.json"},
        "export": lambda: {"--dataset": dataset, "--gold_report_uri": reports + "gold.json",
                           "--pii_report_uri": reports + "silver.json",
                           "--report_uri": reports + kw.get("export_report", "export.json"),
                           "--run_id": kw["run_id"], "--acceptance_uri": landing + "eval_prompts.jsonl"},
    }[step]()


def static_arguments(script: str, account_id: str, data_bucket: str, guardrail_id: str = "",
                     guardrail_version: str = "") -> dict:
    """What every run of a job shares, set as the job's DefaultArguments at deploy time: the
    state machine cannot carry them (07_lambdas substitutes only two placeholders, and the
    account id may not appear in this public repo), and a run's own arguments override them."""
    return {
        "generate_landing.py": {"--data_bucket": data_bucket},
        "land_bronze.py": {"--account_id": account_id, "--raw_bucket": RAW},
        "bronze_to_silver.py": {"--account_id": account_id, "--raw_bucket": RAW, "--curated_bucket": CURATED,
                                "--guardrail_id": guardrail_id, "--guardrail_version": guardrail_version,
                                "--org_entities_uri": f"s3://{data_bucket}/{GLUE_PREFIX}/org_entities.json"},
        "silver_to_gold.py": {"--account_id": account_id, "--curated_bucket": CURATED},
        "export_gold.py": {"--data_bucket": data_bucket, "--curated_bucket": CURATED, "--workgroup": WORKGROUP},
    }[script]


def job_definition(name, script, role_arn, data_bucket, static=None) -> dict:
    extra = ({"--additional-python-modules": JOB_PYTHON_MODULES[script]} if script in JOB_PYTHON_MODULES else {})
    static = static or {}
    if script in PYTHON_SHELL_JOBS:
        return {"Name": name, "Role": role_arn, "GlueVersion": "3.0", "MaxCapacity": 0.0625, "Timeout": 30,
                "Command": {"Name": "pythonshell", "PythonVersion": "3.9",
                            "ScriptLocation": f"s3://{data_bucket}/{GLUE_PREFIX}/{script}"},
                "DefaultArguments": {"--extra-py-files": f"s3://{data_bucket}/{GLUE_PREFIX}/{SHELL_JOB_MODULE[script]}",
                                     "--TempDir": f"s3://{data_bucket}/{GLUE_PREFIX}/tmp/", **static}}
    return {"Name": name, "Role": role_arn, "GlueVersion": "5.0", "WorkerType": "G.1X",
            "NumberOfWorkers": 2, "Timeout": 30,
            "Command": {"Name": "glueetl", "PythonVersion": "3",
                        "ScriptLocation": f"s3://{data_bucket}/{GLUE_PREFIX}/{script}"},
            "DefaultArguments": {"--datalake-formats": "iceberg",
                                 "--extra-py-files": f"s3://{data_bucket}/{GLUE_PREFIX}/transform.py",
                                 "--TempDir": f"s3://{data_bucket}/{GLUE_PREFIX}/tmp/",
                                 "--enable-glue-datacatalog": "true", **extra, **static}}


def ensure_jobs(glue, s3, role_arn, data_bucket, generator_role_arn=None, statics=None) -> dict:
    """Upload the scripts and their modules, then create or update each job (idempotent). An update
    rewrites DefaultArguments, so a newly published guardrail version reaches the next run."""
    out = {}
    s3.put_object(Bucket=data_bucket, Key=f"{GLUE_PREFIX}/transform.py", Body=TRANSFORM_SRC.read_bytes())
    s3.put_object(Bucket=data_bucket, Key=f"{GLUE_PREFIX}/org_entities.json", Body=ORG_ENTITIES_SRC.read_bytes())
    s3.put_object(Bucket=data_bucket, Key=f"{GLUE_PREFIX}/export.py", Body=EXPORT_SRC.read_bytes())
    s3.put_object(Bucket=data_bucket, Key=f"{GLUE_PREFIX}/generate.py", Body=GENERATE_SRC.read_bytes())
    for name, script in JOBS.values():
        s3.put_object(Bucket=data_bucket, Key=f"{GLUE_PREFIX}/{script}", Body=(GLUE_SRC / script).read_bytes())
        role = generator_role_arn if script == "generate_landing.py" and generator_role_arn else role_arn
        d = job_definition(name, script, role, data_bucket, (statics or {}).get(script))
        try:
            glue.get_job(JobName=name)
            glue.update_job(JobName=name, JobUpdate={k: v for k, v in d.items() if k != "Name"})
            out[name] = "updated"
        except glue.exceptions.EntityNotFoundException:
            glue.create_job(**d, Tags=dict(PROJECT_TAG))
            out[name] = "created"
    return out


def ensure_workgroup(athena, data_bucket) -> str:
    try:
        cfg = athena.get_work_group(WorkGroup=WORKGROUP)["WorkGroup"]["Configuration"]
        if "EncryptionConfiguration" in cfg.get("ResultConfiguration", {}):
            athena.update_work_group(WorkGroup=WORKGROUP, ConfigurationUpdates={
                "ResultConfigurationUpdates": {"RemoveEncryptionConfiguration": True}})
            return "updated (result encryption override removed; bucket default applies)"
        return "exists"
    except athena.exceptions.InvalidRequestException:
        athena.create_work_group(Name=WORKGROUP, Configuration=workgroup_config(data_bucket),
                                 Description="llmops lakehouse queries and UNLOAD exports",
                                 Tags=_tags_list())
        return "created"


def data_bucket_name(session, region, account_id, override=None) -> str:
    if override:
        return override
    try:  # the live bucket is authoritative (AGENTS.md: config_subst gotcha)
        return session.client("ssm", region_name=region).get_parameter(
            Name="/llmops/storage/bucket")["Parameter"]["Value"]
    except Exception:  # noqa: BLE001 -- fall back to the derived name, as 03_storage.py does
        return f"llmops-agentic-{account_id}-{region}"


def deploy(session, region, account_id, data_bucket) -> dict:
    glue = session.client("glue", region_name=region)
    try:
        glue.get_catalog(CatalogId=f"{account_id}:s3tablescatalog")
    except glue.exceptions.EntityNotFoundException:
        raise SystemExit("the S3 Tables ↔ Glue Data Catalog integration (s3tablescatalog) is not "
                         "enabled in this Region. It is account-level and persistent, so this "
                         "script does not enable it: see PROJECT_STATE.md")
    kms, s3t = session.client("kms", region_name=region), session.client("s3tables", region_name=region)
    out, keys, arns = {}, {}, {}
    for b in (RAW, CURATED):
        keys[b], out[f"key {b}"] = ensure_key(kms, region, account_id, b)
        arns[b], out[f"bucket {b}"] = ensure_bucket(s3t, b, keys[b])
        for ns in NAMESPACES[b]:
            out[f"namespace {b}/{ns}"] = ensure_namespace(s3t, arns[b], ns)
    for t in TABLES:
        out[f"table {t['namespace']}.{t['name']}"] = ensure_table(s3t, arns[t["bucket"]], t)
    s3t.put_table_bucket_maintenance_configuration(
        tableBucketARN=arns[RAW], type="icebergUnreferencedFileRemoval",
        value={"status": "enabled", "settings": {"icebergUnreferencedFileRemoval": RAW_FILE_REMOVAL}})
    for t in (t for t in TABLES if t["bucket"] == RAW):
        s3t.put_table_maintenance_configuration(
            tableBucketARN=arns[RAW], namespace=t["namespace"], name=t["name"],
            type="icebergSnapshotManagement",
            value={"status": "enabled", "settings": {"icebergSnapshotManagement": RAW_SNAPSHOT}})
    out["maintenance"] = "applied"
    out["workgroup"] = ensure_workgroup(session.client("athena", region_name=region), data_bucket)
    gid, garn, gver, out["guardrail"] = ensure_guardrail(session.client("bedrock", region_name=region))
    role_arn = ensure_role(session.client("iam"), region, account_id, data_bucket, keys, garn)
    out["role"] = role_arn.rsplit("/", 1)[-1]
    gen_arn = ensure_generator_role(session.client("iam"), region, account_id, data_bucket)
    out["generator_role"] = gen_arn.rsplit("/", 1)[-1]
    statics = {script: static_arguments(script, account_id, data_bucket, gid, gver) for _, script in JOBS.values()}
    out["jobs"] = ensure_jobs(glue, session.client("s3", region_name=region), role_arn, data_bucket,
                              gen_arn, statics)
    values = {"raw_bucket_arn": arns[RAW], "curated_bucket_arn": arns[CURATED],
              "raw_kms_key_arn": keys[RAW], "curated_kms_key_arn": keys[CURATED],
              "workgroup": WORKGROUP, "glue_role_arn": role_arn,
              "guardrail_id": gid, "guardrail_version": gver}
    ssm = session.client("ssm", region_name=region)
    for k in SSM_KEYS:
        ssm.put_parameter(Name=f"{SSM_PREFIX}/{k}", Value=values[k], Type="String", Overwrite=True)
    out["ssm"] = f"{len(SSM_KEYS)} parameters under {SSM_PREFIX}/"
    return out


def verify(session, region, account_id, data_bucket) -> dict:
    """Read back what deploy() promised: encryption, format-version, partitioning, retention,
    and an Athena query through the workgroup against every table."""
    s3t = session.client("s3tables", region_name=region)
    s3 = session.client("s3", region_name=region)
    athena = session.client("athena", region_name=region)
    checks = {}
    arns = {b: find_bucket(s3t, b) for b in (RAW, CURATED)}
    for b, arn in arns.items():
        enc = s3t.get_table_bucket_encryption(tableBucketARN=arn)["encryptionConfiguration"]
        checks[f"{b} encryption"] = enc["sseAlgorithm"] == "aws:kms" and bool(enc.get("kmsKeyArn"))
    rules = s3.get_bucket_encryption(Bucket=data_bucket)["ServerSideEncryptionConfiguration"]["Rules"]
    checks["athena results encrypted by bucket default"] = any(
        r.get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm") in ("AES256", "aws:kms", "aws:kms:dsse")
        for r in rules)
    wg = athena.get_work_group(WorkGroup=WORKGROUP)["WorkGroup"]["Configuration"]
    checks["workgroup sets no result encryption"] = "EncryptionConfiguration" not in wg.get("ResultConfiguration", {})
    for t in TABLES:
        loc = s3t.get_table_metadata_location(tableBucketARN=arns[t["bucket"]],
                                              namespace=t["namespace"], name=t["name"])["metadataLocation"]
        b, _, k = loc[len("s3://"):].partition("/")
        meta = json.loads(s3.get_object(Bucket=b, Key=k)["Body"].read())
        spec = next((s for s in meta.get("partition-specs", []) if s.get("spec-id") == meta.get("default-spec-id")), {})
        want = [p[2] for p in t["partition"]]
        got = [f.get("name") for f in spec.get("fields", [])]
        cols = [f["name"] for f in meta["schemas"][-1]["fields"]] if meta.get("schemas") else []
        checks[f"{t['namespace']}.{t['name']}"] = (
            meta.get("format-version") == 2 and got == want and cols == [f["name"] for f in t["fields"]])
        sql = f'SELECT count(*) FROM "{t["namespace"]}"."{t["name"]}"'
        qid = athena.start_query_execution(QueryString=sql, WorkGroup=WORKGROUP, QueryExecutionContext={
            "Catalog": f"s3tablescatalog/{t['bucket']}", "Database": t["namespace"]})["QueryExecutionId"]
        state = _await_query(athena, qid)
        checks[f"athena {t['namespace']}.{t['name']}"] = True if state == "SUCCEEDED" else state
    g = next((g for g in session.client("bedrock", region_name=region).list_guardrails(maxResults=100)
              .get("guardrails", []) if g["name"] == GUARDRAIL_NAME), None)
    checks["guardrail published"] = bool(g)
    checks.update(simulate_glue_role(session.client("iam"), region, account_id, data_bucket, arns,
                                     _key_arns(session.client("kms", region_name=region)),
                                     g["arn"] if g else None))
    checks.update(simulate_generator_role(session.client("iam"), region, account_id, data_bucket, arns,
                                          _key_arns(session.client("kms", region_name=region))))
    jobs = {n: session.client("glue", region_name=region).get_job(JobName=n)["Job"] for n, _ in JOBS.values()}
    gen_job = jobs[JOBS["generate"][0]]
    checks["generator job runs as the generator role"] = gen_job["Role"].endswith(f"/{GENERATOR_ROLE}")
    checks["every job carries its static arguments"] = all(
        static_arguments(script, account_id, data_bucket).keys() <= jobs[name]["DefaultArguments"].keys()
        for name, script in JOBS.values()) or {n: sorted(j["DefaultArguments"]) for n, j in jobs.items()}
    snap = s3t.get_table_maintenance_configuration(tableBucketARN=arns[RAW], namespace="bronze",
                                                   name="conversation_events")["configuration"]
    sm = snap.get("icebergSnapshotManagement", {}).get("settings", {}).get("icebergSnapshotManagement", {})
    checks["bronze snapshot retention"] = sm.get("maxSnapshotAgeHours") == RAW_SNAPSHOT["maxSnapshotAgeHours"]
    return checks


def glue_role_cases(region, account_id, data_bucket, arns, keys, guardrail_arn=None) -> list[tuple[str, str, bool]]:
    """(action, resource, should_be_allowed) for the Glue role: what it needs, and what it must
    not have. Asked of IAM as the role (simulate_principal_policy), not inferred from the JSON."""
    cat = f"arn:aws:glue:{region}:{account_id}"
    bronze = f"{arns[RAW]}/table/{'0' * 8}-0000-0000-0000-{'0' * 12}"
    return [
        ("s3tables:PutTableData", bronze, True),
        ("s3tables:UpdateTableMetadataLocation", f"{arns[CURATED]}/table/x", True),
        ("glue:UpdateTable", f"{cat}:table/s3tablescatalog/{CURATED}/silver/instruction_pairs", True),
        ("kms:Decrypt", keys[RAW], True),
        ("kms:GenerateDataKey", keys[CURATED], True),
        ("s3:PutObject", f"arn:aws:s3:::{data_bucket}/{GLUE_PREFIX}/jobs/x.py", True),
        ("s3tables:DeleteTable", bronze, False),
        ("s3tables:DeleteTableBucket", arns[RAW], False),
        ("s3tables:PutTableData", f"arn:aws:s3tables:{region}:{account_id}:bucket/other/table/x", False),
        ("glue:DeleteTable", f"{cat}:table/s3tablescatalog/{RAW}/bronze/feedback", False),
        ("kms:ScheduleKeyDeletion", keys[RAW], False),
        ("s3:GetObject", f"arn:aws:s3:::{data_bucket}/customer-data/x", False),
        ("iam:PassRole", f"arn:aws:iam::{account_id}:role/{GLUE_ROLE}", False),
        ("s3:GetObject", f"arn:aws:s3:::{data_bucket}/{LANDING_PREFIX}/d/bronze_feedback.jsonl", True),
        ("s3:PutObject", f"arn:aws:s3:::{data_bucket}/{REPORT_PREFIX}/d/silver.json", True),
        ("s3:PutObject", f"arn:aws:s3:::{data_bucket}/{LANDING_PREFIX}/d/x", False),
        ("athena:StartQueryExecution", f"arn:aws:athena:{region}:{account_id}:workgroup/{WORKGROUP}", True),
        ("athena:StartQueryExecution", f"arn:aws:athena:{region}:{account_id}:workgroup/primary", False),
        ("s3:PutObject", f"arn:aws:s3:::{data_bucket}/{EXTRACTS_PREFIX}/d/1/train.jsonl", True),
        ("s3:DeleteObject", f"arn:aws:s3:::{data_bucket}/{EXTRACTS_PREFIX}/d/1/train.jsonl", False),
    ] + ([("bedrock:ApplyGuardrail", guardrail_arn, True),
          ("bedrock:ApplyGuardrail", f"arn:aws:bedrock:{region}:{account_id}:guardrail/someone-elses", False)]
         if guardrail_arn else [])


def generator_role_cases(region, account_id, data_bucket, arns, keys) -> list[tuple[str, str, bool]]:
    """The generator may write landing and must not touch anything downstream of it."""
    bronze = f"{arns[RAW]}/table/{'0' * 8}-0000-0000-0000-{'0' * 12}"
    return [
        ("s3:PutObject", f"arn:aws:s3:::{data_bucket}/{LANDING_PREFIX}/d/bronze_feedback.jsonl", True),
        ("s3:GetObject", f"arn:aws:s3:::{data_bucket}/{GLUE_PREFIX}/generate.py", True),
        ("s3:PutObject", f"arn:aws:s3:::{data_bucket}/{REPORT_PREFIX}/d/silver.json", False),
        ("s3:PutObject", f"arn:aws:s3:::{data_bucket}/{EXTRACTS_PREFIX}/d/1/train.jsonl", False),
        ("s3:PutObject", f"arn:aws:s3:::{data_bucket}/{GLUE_PREFIX}/land_bronze.py", False),
        ("s3:GetObject", f"arn:aws:s3:::{data_bucket}/customer-data/x", False),
        ("s3:DeleteObject", f"arn:aws:s3:::{data_bucket}/{LANDING_PREFIX}/d/bronze_feedback.jsonl", False),
        ("s3tables:PutTableData", bronze, False),
        ("kms:Decrypt", keys[RAW], False),
    ]


def simulate_generator_role(iam, region, account_id, data_bucket, arns, keys) -> dict:
    role = f"arn:aws:iam::{account_id}:role/{GENERATOR_ROLE}"
    out = {}
    for action, resource, want in generator_role_cases(region, account_id, data_bucket, arns, keys):
        got = iam.simulate_principal_policy(PolicySourceArn=role, ActionNames=[action], ResourceArns=[resource]
                                            )["EvaluationResults"][0]["EvalDecision"] == "allowed"
        label = (resource.split(":::", 1)[1].split("/", 1)[-1] if resource.startswith("arn:aws:s3:::")
                 else resource.split(":")[-1])[:60]
        out[f"generator role may{'' if want else ' NOT'} {action} on {label}"] = got == want or f"got {got}"
    return out


def simulate_glue_role(iam, region, account_id, data_bucket, arns, keys, guardrail_arn=None) -> dict:
    role = f"arn:aws:iam::{account_id}:role/{GLUE_ROLE}"
    out = {}
    for action, resource, want in glue_role_cases(region, account_id, data_bucket, arns, keys, guardrail_arn):
        r = iam.simulate_principal_policy(PolicySourceArn=role, ActionNames=[action],
                                          ResourceArns=[resource])["EvaluationResults"][0]
        allowed = r["EvalDecision"] == "allowed"
        # The resource part only: an ARN carries the account, and an S3 ARN's bucket name embeds it.
        label = (resource.split(":::", 1)[1].split("/", 1)[-1] if resource.startswith("arn:aws:s3:::")
                 else resource.split(":")[-1])[:60]
        out[f"glue role {'may' if want else 'may NOT'} {action} on {label}"] = (
            True if allowed == want else f"{r['EvalDecision']} on {resource.rsplit(':', 1)[-1]}")
    return out


def _key_arns(kms) -> dict:
    return {b: kms.describe_key(KeyId=KEY_ALIAS[b])["KeyMetadata"]["Arn"] for b in (RAW, CURATED)}


def _await_query(athena, qid, timeout_s=120) -> str:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        st = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        if st["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            return st["State"] if st["State"] == "SUCCEEDED" else f"{st['State']}: {st.get('StateChangeReason', '')}"
        time.sleep(2)
    return "TIMEOUT"


def teardown(session, region, account_id) -> dict:
    """Everything deploy() creates, in reverse order. Tables first (a bucket with tables cannot
    be deleted); keys are SCHEDULED for deletion, never deleted outright."""
    s3t, out = session.client("s3tables", region_name=region), {}
    ssm = session.client("ssm", region_name=region)
    for k in SSM_KEYS:
        try:
            ssm.delete_parameter(Name=f"{SSM_PREFIX}/{k}")
        except ssm.exceptions.ParameterNotFound:
            pass
    glue, gone = session.client("glue", region_name=region), 0
    for name, _ in JOBS.values():
        try:
            glue.get_job(JobName=name)
            glue.delete_job(JobName=name)
            gone += 1
        except glue.exceptions.EntityNotFoundException:
            pass
    out["jobs"] = f"{gone} deleted" if gone else "absent"
    bedrock = session.client("bedrock", region_name=region)
    g = next((g for g in bedrock.list_guardrails(maxResults=100).get("guardrails", [])
              if g["name"] == GUARDRAIL_NAME), None)
    if g:
        bedrock.delete_guardrail(guardrailIdentifier=g["id"])
    out["guardrail"] = "deleted" if g else "absent"
    iam = session.client("iam")
    for key, role, policy in (("role", GLUE_ROLE, "lakehouse"), ("generator_role", GENERATOR_ROLE, "lakehouse-generator")):
        try:
            iam.delete_role_policy(RoleName=role, PolicyName=policy)
            iam.delete_role(RoleName=role)
            out[key] = "deleted"
        except iam.exceptions.NoSuchEntityException:
            out[key] = "absent"
    athena = session.client("athena", region_name=region)
    try:
        athena.delete_work_group(WorkGroup=WORKGROUP, RecursiveDeleteOption=True)
        out["workgroup"] = "deleted"
    except athena.exceptions.InvalidRequestException:
        out["workgroup"] = "absent"
    for b in (CURATED, RAW):
        arn = find_bucket(s3t, b)
        if not arn:
            out[f"bucket {b}"] = "absent"
            continue
        for t in (t for t in TABLES if t["bucket"] == b):
            try:
                s3t.delete_table(tableBucketARN=arn, namespace=t["namespace"], name=t["name"])
            except s3t.exceptions.NotFoundException:
                pass
        for ns in NAMESPACES[b]:
            try:
                s3t.delete_namespace(tableBucketARN=arn, namespace=ns)
            except s3t.exceptions.NotFoundException:
                pass
        s3t.delete_table_bucket(tableBucketARN=arn)
        out[f"bucket {b}"] = "deleted"
    kms = session.client("kms", region_name=region)
    for b in (CURATED, RAW):
        try:
            key_id = kms.describe_key(KeyId=KEY_ALIAS[b])["KeyMetadata"]["KeyId"]
        except kms.exceptions.NotFoundException:
            out[f"key {b}"] = "absent"
            continue
        kms.delete_alias(AliasName=KEY_ALIAS[b])
        kms.schedule_key_deletion(KeyId=key_id, PendingWindowInDays=KEY_DELETION_DAYS)
        out[f"key {b}"] = f"scheduled for deletion in {KEY_DELETION_DAYS} days"
    return out


def _load_module(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_job(glue, name, arguments, timeout_s=1800, sleep=time.sleep) -> tuple[str, str]:
    """A run that has just reported SUCCEEDED can still count against the job's concurrency for a
    while: the smoke's back-to-back exports met ConcurrentRunsExceeded on the second start. Only
    that error is retried; anything else is the job's problem and propagates."""
    for attempt in range(6):
        try:
            run_id = glue.start_job_run(JobName=name, Arguments=arguments)["JobRunId"]
            break
        except glue.exceptions.ConcurrentRunsExceededException:
            if attempt == 5:
                raise
            sleep(15 * (attempt + 1))
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        jr = glue.get_job_run(JobName=name, RunId=run_id)["JobRun"]
        if jr["JobRunState"] in ("SUCCEEDED", "FAILED", "ERROR", "TIMEOUT", "STOPPED"):
            return jr["JobRunState"], jr.get("ErrorMessage", "")[:300]
        sleep(20)
    return "TIMEOUT", ""


def smoke(session, region, account_id, data_bucket, seed=7, scale="small") -> dict:
    """Generate → land → Bronze→Silver → Silver→Gold → export through the deployed jobs, then hold
    the Silver rule report to the generator's plants exactly. Every job gets only its per-run
    arguments (`run_arguments`, the layout the state machine mirrors); everything else must come
    from the job's DefaultArguments, so a smoke that passes proves those are deployed. The
    pipeline mode that runs the same chain is `lakehouse_ingest` (PR-09)."""
    import tempfile
    gen = _load_module("lh_generate", GENERATE_SRC)
    tf = _load_module("lh_transform", TRANSFORM_SRC)
    s3, glue = session.client("s3", region_name=region), session.client("glue", region_name=region)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    dataset = f"seed{seed}-{scale}-{stamp}"
    ds = gen.generate(seed, scale)
    with tempfile.TemporaryDirectory() as tmp:
        local = gen.write(ds, __import__("pathlib").Path(tmp))  # the 3.12 digest the job must match
    checks = {"dataset": dataset, "dataset_sha256": local["dataset_sha256"]}
    reports = f"s3://{data_bucket}/{REPORT_PREFIX}/{dataset}/"
    state, err = _run_job(glue, JOBS["generate"][0], run_arguments("generate", data_bucket, dataset,
                                                                   seed=seed, scale=scale))
    checks[f"job {JOBS['generate'][0]}"] = True if state == "SUCCEEDED" else f"{state}: {err}"
    if state != "SUCCEEDED":
        return checks
    landed = json.loads(s3.get_object(Bucket=data_bucket, Key=f"{LANDING_PREFIX}/{dataset}/manifest.json")["Body"].read())
    checks["generator job digest = local digest (Python 3.9 vs 3.12)"] = (
        landed["dataset_sha256"] == local["dataset_sha256"] or landed["dataset_sha256"][:16])
    for key in ("land", "silver", "gold"):
        state, err = _run_job(glue, JOBS[key][0], run_arguments(key, data_bucket, dataset))
        checks[f"job {JOBS[key][0]}"] = True if state == "SUCCEEDED" else f"{state}: {err}"
        if state != "SUCCEEDED":
            return checks
    exports = []
    for attempt in (1, 2):  # twice: the second must reproduce the first byte for byte
        state, err = _run_job(glue, JOBS["export"][0], run_arguments(
            "export", data_bucket, dataset, run_id=f"smoke-{dataset}", export_report=f"export-{attempt}.json"))
        checks[f"job {JOBS['export'][0]} (run {attempt})"] = True if state == "SUCCEEDED" else f"{state}: {err}"
        if state != "SUCCEEDED":
            return checks
        exports.append(json.loads(s3.get_object(
            Bucket=data_bucket, Key=f"{REPORT_PREFIX}/{dataset}/export-{attempt}.json")["Body"].read()))
    acc = exports[1]["files"].get("acceptance") or exports[1].get("acceptance")
    checks["acceptance set exported with every held-out item"] = (
        (acc or {}).get("rows") == len(ds["eval_prompts"]) and bool((acc or {}).get("sha256"))) or acc
    checks["export reproducible (same sha256 twice)"] = all(
        exports[0]["files"][k]["sha256"] == exports[1]["files"][k]["sha256"] for k in ("train", "eval", "acceptance")
        if k in exports[0]["files"]) or [
        (e["files"]["train"]["sha256"][:12], e["files"]["eval"]["sha256"][:12]) for e in exports]
    driver = _load_module("lh_driver", REPO / "orchestration" / "harness_driver" / "handler.py")
    for split in ("train", "eval"):
        f = exports[1]["files"][split]
        att = driver._verify_gold_extract(s3, data_bucket, {"extract_uri": f["uri"], "extract_sha256": f["sha256"]})
        checks[f"driver re-verifies the {split} extract"] = att["verified"] is True or att
    silver = json.loads(s3.get_object(Bucket=data_bucket, Key=f"{REPORT_PREFIX}/{dataset}/silver.json")["Body"].read())
    gold = json.loads(s3.get_object(Bucket=data_bucket, Key=f"{REPORT_PREFIX}/{dataset}/gold.json")["Body"].read())
    for rule, (ok, detail) in tf.compare_to_plants(silver["rules"], ds["expected_defects"]).items():
        checks[f"rule {rule}"] = True if ok else detail
    checks["silver dq"] = all(o["outcome"] == "Passed" for o in silver["dq"]["outcomes"]) or silver["dq"]
    checks["gold dq"] = all(o["outcome"] == "Passed" for v in gold["dq"].values() for o in v) or gold["dq"]
    athena = session.client("athena", region_name=region)
    checks["export rows = gold rows"] = (exports[1]["files"]["train"]["rows"], exports[1]["files"]["eval"]["rows"]) == (
        gold["train"], gold["eval"]) or exports[1]["files"]
    for bucket, ns, table, want in ((CURATED, "silver", "instruction_pairs", silver["pairs"]),
                                    (CURATED, "gold", "qlora_train", gold["train"]),
                                    (CURATED, "gold", "qlora_eval", gold["eval"])):
        qid = athena.start_query_execution(
            QueryString=f'SELECT count(*) FROM "{ns}"."{table}"', WorkGroup=WORKGROUP,
            QueryExecutionContext={"Catalog": f"s3tablescatalog/{bucket}", "Database": ns})["QueryExecutionId"]
        state = _await_query(athena, qid)
        got = (int(athena.get_query_results(QueryExecutionId=qid)["ResultSet"]["Rows"][1]["Data"][0]["VarCharValue"])
               if state == "SUCCEEDED" else state)
        checks[f"athena {ns}.{table} rows"] = True if got == want else f"{got} != {want}"
    checks["silver pairs"], checks["gold train/eval"] = silver["pairs"], f"{gold['train']}/{gold['eval']}"
    checks["extract"] = f"{exports[1]['prefix']} train {exports[1]['files']['train']['sha256'][:16]}…"
    return checks


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--region", required=True)
    ap.add_argument("--account-id", help="skip STS (offline dry-run) and use this account id")
    ap.add_argument("--bucket", help="data bucket (default: SSM /llmops/storage/bucket)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan; no AWS call when "
                    "--account-id and --bucket are given")
    ap.add_argument("--verify", action="store_true", help="read back encryption, format, "
                    "partitioning, retention and an Athena query per table")
    ap.add_argument("--smoke", action="store_true", help="generate, land, run the three jobs and "
                    "hold the rule report to the generator's plants (billable Glue runs)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--scale", default="small", choices=("small", "demo"))
    ap.add_argument("--teardown", action="store_true",
                    help="delete what this script creates (tables and their data included)")
    args = ap.parse_args(argv)
    validate_tables()
    session = None
    if not (args.dry_run and args.account_id and args.bucket):
        import boto3
        session = boto3.Session(region_name=args.region)
    account_id = args.account_id or session.client("sts").get_caller_identity()["Account"]
    data_bucket = args.bucket or data_bucket_name(session, args.region, account_id)
    if args.teardown:
        if args.dry_run:
            print("would delete: ssm params, role, workgroup, every table and namespace, both "
                  f"table buckets; would schedule both keys for deletion ({KEY_DELETION_DAYS} days)")
            return 0
        print(json.dumps(teardown(session, args.region, account_id), indent=2))
        return 0
    if args.dry_run:
        for item in plan(args.region, account_id, data_bucket):
            print(f"would ensure {item['kind']}: {item['name']}")
        return 0
    if args.smoke:
        checks = smoke(session, args.region, account_id, data_bucket, args.seed, args.scale)
        for k, v in checks.items():
            if isinstance(v, bool):
                print(f"{'PASS' if v else 'FAIL'}  {k}")
            else:
                print(f"{'INFO' if k in ('dataset', 'dataset_sha256', 'silver pairs', 'gold train/eval', 'extract') else 'FAIL'}  {k}: {v}")
        return 0 if all(v is True for k, v in checks.items()
                        if k not in ("dataset", "dataset_sha256", "silver pairs", "gold train/eval", "extract")) else 1
    if args.verify:
        checks = verify(session, args.region, account_id, data_bucket)
        for k, v in checks.items():
            print(f"{'PASS' if v is True else 'FAIL'}  {k}" + ("" if isinstance(v, bool) else f": {v}"))
        return 0 if all(v is True for v in checks.values()) else 1
    print(json.dumps(deploy(session, args.region, account_id, data_bucket), indent=2))
    print("STANDING COST: 2 KMS keys ($1/month each) + S3 Tables storage for data that lands. "
          f"Teardown: python deploy/10_lakehouse.py --region {args.region} --teardown")
    return 0


if __name__ == "__main__":
    sys.exit(main())
