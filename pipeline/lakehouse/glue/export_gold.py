"""Glue Python-shell job: Gold → training extract (Phase 1 PR-08).

Reads the Gold snapshot ids from the silver_to_gold report, UNLOADs each split FOR VERSION AS OF that
snapshot, writes `train.jsonl` / `val.jsonl` + `extract.json` under
s3://<DATA_BUCKET>/lakehouse/extracts/<dataset>/<train snapshot>/, records each file in
gold.dataset_manifest and writes an export report the console and the smoke check read. All of the
logic that decides the bytes is pipeline/lakehouse/export.py (shipped with --extra-py-files).

Arguments: --data_bucket --curated_bucket --workgroup --dataset --gold_report_uri --report_uri
           --pii_report_uri (Silver report: what the PII rules did to these rows) [--run_id]
           [--acceptance_uri] (the generator's eval_prompts.jsonl: the held-out acceptance set)
"""
import datetime
import json
import sys
import time
import uuid

import boto3
from awsglue.utils import getResolvedOptions

import export

args = getResolvedOptions(sys.argv, ["data_bucket", "curated_bucket", "workgroup", "dataset",
                                     "gold_report_uri", "report_uri", "pii_report_uri"])
run_id = getResolvedOptions(sys.argv, ["run_id"])["run_id"] if "--run_id" in sys.argv else None
acceptance_uri = (getResolvedOptions(sys.argv, ["acceptance_uri"])["acceptance_uri"]
                  if "--acceptance_uri" in sys.argv else None)
s3, athena = boto3.client("s3"), boto3.client("athena")
ctx = {"Catalog": f"s3tablescatalog/{args['curated_bucket']}", "Database": "gold"}


def _get(uri):
    b, _, k = uri[len("s3://"):].partition("/")
    return s3.get_object(Bucket=b, Key=k)["Body"].read()


def _query(sql):
    qid = athena.start_query_execution(QueryString=sql, WorkGroup=args["workgroup"],
                                       QueryExecutionContext=ctx)["QueryExecutionId"]
    while True:
        st = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        if st["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            if st["State"] != "SUCCEEDED":
                raise SystemExit(f"Athena {st['State']}: {st.get('StateChangeReason', '')}")
            return qid
        time.sleep(2)


gold = json.loads(_get(args["gold_report_uri"]))
snapshots = {split: int(gold["tables"][f"gold.{table}"]["snapshot_id"]) for split, table in export.SPLITS.items()}
prefix = export.extract_prefix(args["dataset"], snapshots["train"])
now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
report = {"dataset": args["dataset"], "prefix": f"s3://{args['data_bucket']}/{prefix}/", "files": {},
          "created_ts": now}
for split, table in export.SPLITS.items():
    unload = f"{prefix}/unload/{table}-{uuid.uuid4().hex[:12]}/"   # a fresh prefix: UNLOAD refuses a used one
    _query(export.unload_sql(table, snapshots[split], f"s3://{args['data_bucket']}/{unload}"))
    keys = [o["Key"] for p in s3.get_paginator("list_objects_v2").paginate(Bucket=args["data_bucket"], Prefix=unload)
            for o in p.get("Contents", [])]
    rows = export.rows_from_unload([s3.get_object(Bucket=args["data_bucket"], Key=k)["Body"].read() for k in keys])
    body = export.to_jsonl(rows)
    key = f"{prefix}/{export.FILE_FOR_SPLIT[split]}"
    s3.put_object(Bucket=args["data_bucket"], Key=key, Body=body, ContentType="application/x-ndjson")
    digest = export.sha256(body)
    uri = f"s3://{args['data_bucket']}/{key}"
    report["files"][split] = {"uri": uri, "sha256": digest, "rows": len(rows),
                              "table": f"gold.{table}", "snapshot_id": str(snapshots[split])}
    _query(export.manifest_insert_sql({
        "dataset_version": args["dataset"], "table_name": f"gold.{table}", "snapshot_id": snapshots[split],
        "export_uri": uri, "sha256": digest, "row_count": len(rows), "dq_score": None,
        "pii_report_uri": args["pii_report_uri"], "run_id": run_id, "created_ts": now}))

# The held-out acceptance set rides in the same bundle, hashed like the splits, so the set a gate
# is anchored to is as checkable as the data the model trained on. It is copied from landing, not
# from a table: it is never trained on, so it never enters Bronze.
if acceptance_uri:
    body, n, why = export.acceptance_jsonl(_get(acceptance_uri))
    if body is None:
        report["acceptance"] = {"source": acceptance_uri, "exported": False, "reason": why, "rows": n}
    else:
        key = f"{prefix}/{export.ACCEPTANCE_FILE}"
        s3.put_object(Bucket=args["data_bucket"], Key=key, Body=body, ContentType="application/x-ndjson")
        report["files"]["acceptance"] = {"uri": f"s3://{args['data_bucket']}/{key}", "sha256": export.sha256(body),
                                         "rows": n, "source": acceptance_uri}

s3.put_object(Bucket=args["data_bucket"], Key=f"{prefix}/extract.json", ContentType="application/json",
              Body=json.dumps(report, indent=2).encode())
b, _, k = args["report_uri"][len("s3://"):].partition("/")
s3.put_object(Bucket=b, Key=k, ContentType="application/json", Body=json.dumps(report, indent=2).encode())
print(json.dumps(report))
