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
GLUE_PREFIX = "lakehouse/glue"
ATHENA_PREFIX = "athena/lakehouse"
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


def glue_role_policy(region: str, account_id: str, data_bucket: str, key_arns: dict) -> dict:
    """The S7-proven grants, widened from one spike bucket to the two lakehouse buckets."""
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
    ]}


def workgroup_config(data_bucket: str) -> dict:
    return {"ResultConfiguration": {
                "OutputLocation": f"s3://{data_bucket}/{ATHENA_PREFIX}/",
                "EncryptionConfiguration": {"EncryptionOption": "SSE_S3"}},
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
    items.append({"kind": "iam role", "name": f"arn:aws:iam::{account_id}:role/{GLUE_ROLE}"})
    items += [{"kind": "ssm parameter", "name": f"{SSM_PREFIX}/{k}"} for k in SSM_KEYS]
    return items


SSM_KEYS = ("raw_bucket_arn", "curated_bucket_arn", "raw_kms_key_arn", "curated_kms_key_arn",
            "workgroup", "glue_role_arn")


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


def ensure_role(iam, region, account_id, data_bucket, key_arns) -> str:
    try:
        arn = iam.get_role(RoleName=GLUE_ROLE)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=GLUE_ROLE, AssumeRolePolicyDocument=json.dumps(
            glue_trust_policy(account_id)), Tags=_tags_list(),
            Description="llmops lakehouse Glue jobs (Bronze->Silver->Gold)")["Role"]["Arn"]
    iam.put_role_policy(RoleName=GLUE_ROLE, PolicyName="lakehouse", PolicyDocument=json.dumps(
        glue_role_policy(region, account_id, data_bucket, key_arns)))
    return arn


def ensure_workgroup(athena, data_bucket) -> str:
    try:
        athena.get_work_group(WorkGroup=WORKGROUP)
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
    role_arn = ensure_role(session.client("iam"), region, account_id, data_bucket, keys)
    out["role"] = role_arn.rsplit("/", 1)[-1]
    values = {"raw_bucket_arn": arns[RAW], "curated_bucket_arn": arns[CURATED],
              "raw_kms_key_arn": keys[RAW], "curated_kms_key_arn": keys[CURATED],
              "workgroup": WORKGROUP, "glue_role_arn": role_arn}
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
    checks.update(simulate_glue_role(session.client("iam"), region, account_id, data_bucket, arns,
                                     _key_arns(session.client("kms", region_name=region))))
    snap = s3t.get_table_maintenance_configuration(tableBucketARN=arns[RAW], namespace="bronze",
                                                   name="conversation_events")["configuration"]
    sm = snap.get("icebergSnapshotManagement", {}).get("settings", {}).get("icebergSnapshotManagement", {})
    checks["bronze snapshot retention"] = sm.get("maxSnapshotAgeHours") == RAW_SNAPSHOT["maxSnapshotAgeHours"]
    return checks


def glue_role_cases(region, account_id, data_bucket, arns, keys) -> list[tuple[str, str, bool]]:
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
    ]


def simulate_glue_role(iam, region, account_id, data_bucket, arns, keys) -> dict:
    role = f"arn:aws:iam::{account_id}:role/{GLUE_ROLE}"
    out = {}
    for action, resource, want in glue_role_cases(region, account_id, data_bucket, arns, keys):
        r = iam.simulate_principal_policy(PolicySourceArn=role, ActionNames=[action],
                                          ResourceArns=[resource])["EvaluationResults"][0]
        allowed = r["EvalDecision"] == "allowed"
        out[f"glue role {'may' if want else 'may NOT'} {action}"] = (
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
    iam = session.client("iam")
    try:
        iam.delete_role_policy(RoleName=GLUE_ROLE, PolicyName="lakehouse")
        iam.delete_role(RoleName=GLUE_ROLE)
        out["role"] = "deleted"
    except iam.exceptions.NoSuchEntityException:
        out["role"] = "absent"
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--region", required=True)
    ap.add_argument("--account-id", help="skip STS (offline dry-run) and use this account id")
    ap.add_argument("--bucket", help="data bucket (default: SSM /llmops/storage/bucket)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan; no AWS call when "
                    "--account-id and --bucket are given")
    ap.add_argument("--verify", action="store_true", help="read back encryption, format, "
                    "partitioning, retention and an Athena query per table")
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
