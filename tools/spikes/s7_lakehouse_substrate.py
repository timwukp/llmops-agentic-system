#!/usr/bin/env python3
"""S7 — the S3 Tables substrate Phase 1 is built on.

Questions (research report §13; Phase 1 PR-04/PR-05 are gated on the answers):
  * Does a table created through the S3 Tables API come out as Iceberg format-version 2?
    Phase 1 must stay on v2: Athena cannot read v3 and the upgrade is one-way.
  * Can a Glue 5 Spark job write to it through the Glue Data Catalog's `s3tablescatalog`
    federation with IAM-only grants, or is Lake Formation required?
  * Is `UNLOAD (... FOR VERSION AS OF <snapshot>)` deterministic — two exports of the same
    snapshot hash identically — and does an UNLOAD into a non-empty prefix fail, as the Athena
    docs say? The whole training-reproducibility design (report §4.4) rests on both.
  * Unified Studio: does the table appear in an IAM-based domain's project, does Glue lineage
    reach it, and what does a domain cost? (`--part domain`, implemented in a follow-up PR.)

    python3 tools/spikes/s7_lakehouse_substrate.py plan --account-id <ACCOUNT_ID>
    python3 tools/spikes/s7_lakehouse_substrate.py run            # substrate part
    python3 tools/spikes/s7_lakehouse_substrate.py sweep

Creates, then deletes: a table bucket, a namespace and a table; an IAM role for Glue; a Glue
job and its script under s3://<DATA_BUCKET>/spikes/s7/; an Athena workgroup; UNLOAD outputs.
Persistent and NOT deleted: the account-level S3 Tables ↔ Glue Data Catalog integration, if
this spike finds it missing it records that and stops rather than enabling it.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _spike import Spike, main  # noqa: E402

#: Table bucket name PREFIX. Each run appends a UTC timestamp: a deleted table bucket's name
#: stays in a "transitional state" for a while, so CreateTableBucket on the same name right
#: after a teardown fails with ConflictException (observed live, run 2, 2026-10-13). Leftovers
#: are found by the spike= tag, so nothing depends on the name being fixed.
BUCKET_PREFIX = "llmops-spike-s7"
NAMESPACE = "spike"
TABLE = "events"
GLUE_ROLE = "llmops-spike-s7-glue"
GLUE_JOB = "llmops-spike-s7-write"
WORKGROUP = "llmops-spike-s7"
PREFIX = "spikes/s7"
ROWS = 1000

SCHEMA = {"fields": [
    {"name": "id", "type": "long", "required": True},
    {"name": "lang", "type": "string"},
    {"name": "payload", "type": "string"},
]}

#: Glue 5 job body. Writes ROWS deterministic rows through the s3tablescatalog federation.
GLUE_SCRIPT = '''
import sys
from awsglue.utils import getResolvedOptions
from pyspark.sql import SparkSession
args = getResolvedOptions(sys.argv, ["catalog_id", "rows"])
spark = (SparkSession.builder
    .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
    .config("spark.sql.catalog.s3t", "org.apache.iceberg.spark.SparkCatalog")
    .config("spark.sql.catalog.s3t.catalog-impl", "org.apache.iceberg.aws.glue.GlueCatalog")
    .config("spark.sql.catalog.s3t.glue.id", args["catalog_id"])
    .getOrCreate())
n = int(args["rows"])
rows = [(i, "zh-TW" if i % 2 else "en", "row-%05d" % i) for i in range(n)]
spark.createDataFrame(rows, "id long, lang string, payload string") \\
    .writeTo("s3t.%s.%s" % ("NAMESPACE", "TABLE")).append()
'''.replace('"NAMESPACE"', repr(NAMESPACE)).replace('"TABLE"', repr(TABLE))


class S7LakehouseSubstrate(Spike):
    NAME = "s7_lakehouse_substrate"
    NUMBER = 7
    QUESTION = ("S3 Tables substrate: Iceberg v2, Glue 5 IAM-mode writes, deterministic "
                "UNLOAD FOR VERSION AS OF, Unified Studio visibility")
    GATES = "Phase 1 PR-04 (lakehouse storage) and PR-05 (Unified Studio)"
    PASS_CRITERIA = (
        ("v2", "A table created via the S3 Tables API has Iceberg format-version 2"),
        ("integration", "The account's S3 Tables ↔ Glue Data Catalog integration is enabled"),
        ("glue_write", f"A Glue 5 job writes {ROWS} rows with IAM-only grants (no Lake Formation)"),
        ("snapshot", "The write produces a current snapshot id readable from table metadata"),
        ("unload_det", "Two UNLOADs of the same snapshot produce identical content hashes"),
        ("unload_count", f"The UNLOAD export contains exactly {ROWS} rows"),
        ("unload_nonempty", "An UNLOAD into a non-empty prefix fails"),
        ("smus_visible", "The table is visible in a Unified Studio IAM-based domain project"),
        ("smus_lineage", "A Glue lineage node for the write appears in the domain"),
        ("smus_price", "The domain's price is read from the AWS Price List API"),
    )
    IMPLEMENTED = True  # substrate part; the smus_* criteria stay NOT RUN until --part domain

    def __init__(self, region, account_id=None, session=None, bucket=None):
        super().__init__(region, account_id, session)
        # date-time split by a hyphen: a 14-digit stamp would be a digit run in the evidence
        self.bucket = bucket or f"{BUCKET_PREFIX}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"

    def plan(self) -> list[dict]:
        acct = self.account_id or "<ACCOUNT_ID>"
        return [
            {"kind": "s3tables bucket", "name": self.bucket},
            {"kind": "s3tables namespace", "name": f"{self.bucket}/{NAMESPACE}"},
            {"kind": "s3tables table", "name": f"{self.bucket}/{NAMESPACE}/{TABLE} (ICEBERG)"},
            {"kind": "iam role", "name": f"arn:aws:iam::{acct}:role/{GLUE_ROLE}"},
            {"kind": "s3 objects", "name": f"s3://<DATA_BUCKET>/{PREFIX}/"},
            {"kind": "glue job", "name": f"{GLUE_JOB} (Glue 5.0, 2 x G.1X)"},
            {"kind": "athena workgroup", "name": WORKGROUP},
        ]

    # ── run ──────────────────────────────────────────────────────────────────
    def run(self) -> None:
        sts, ssm = self.client("sts"), self.client("ssm")
        self.account_id = self.account_id or sts.get_caller_identity()["Account"]
        data_bucket = ssm.get_parameter(Name="/llmops/storage/bucket")["Parameter"]["Value"]
        self.redactions.append((data_bucket, "<DATA_BUCKET>"))
        s3t, s3 = self.client("s3tables"), self.client("s3")
        self.track("s3 prefix", f"s3://{data_bucket}/{PREFIX}/",
                   lambda: _delete_prefix(s3, data_bucket, PREFIX))

        bucket_arn = s3t.create_table_bucket(name=self.bucket, tags=self.tags)["arn"]
        self.observe("table_bucket", self.bucket)
        self.track("s3tables bucket", self.bucket,
                   lambda: s3t.delete_table_bucket(tableBucketARN=bucket_arn))
        s3t.create_namespace(tableBucketARN=bucket_arn, namespace=[NAMESPACE])
        self.track("s3tables namespace", NAMESPACE,
                   lambda: s3t.delete_namespace(tableBucketARN=bucket_arn, namespace=NAMESPACE))
        s3t.create_table(tableBucketARN=bucket_arn, namespace=NAMESPACE, name=TABLE,
                         format="ICEBERG", metadata={"iceberg": {"schema": SCHEMA}})
        self.track("s3tables table", TABLE, lambda: s3t.delete_table(
            tableBucketARN=bucket_arn, namespace=NAMESPACE, name=TABLE))

        meta = self._metadata(s3t, s3, bucket_arn)
        self.observe("format_version", meta.get("format-version"))
        self.record("v2", meta.get("format-version") == 2, f"format-version={meta.get('format-version')}")

        glue = self.client("glue")
        catalog_id = f"{self.account_id}:s3tablescatalog/{self.bucket}"
        try:
            glue.get_catalog(CatalogId=f"{self.account_id}:s3tablescatalog")
            self.record("integration", True, "s3tablescatalog federated catalog present")
        except glue.exceptions.EntityNotFoundException:
            self.record("integration", False, "s3tablescatalog not found: enable the S3 Tables "
                        "analytics integration once per Region (persistent, account-level)")
            return

        role_arn = self._glue_role(bucket_arn, data_bucket)
        key = f"{PREFIX}/glue_write.py"
        s3.put_object(Bucket=data_bucket, Key=key, Body=GLUE_SCRIPT.encode())
        glue.create_job(
            Name=GLUE_JOB, Role=role_arn, GlueVersion="5.0", WorkerType="G.1X", NumberOfWorkers=2,
            Command={"Name": "glueetl", "ScriptLocation": f"s3://{data_bucket}/{key}",
                     "PythonVersion": "3"},
            DefaultArguments={"--datalake-formats": "iceberg",
                              "--TempDir": f"s3://{data_bucket}/{PREFIX}/tmp/"},
            Tags=self.tags)
        self.track("glue job", GLUE_JOB, lambda: glue.delete_job(JobName=GLUE_JOB))
        run_id = glue.start_job_run(JobName=GLUE_JOB, Arguments={
            "--catalog_id": catalog_id, "--rows": str(ROWS)})["JobRunId"]
        state, err = _wait_glue(glue, GLUE_JOB, run_id)
        self.record("glue_write", state == "SUCCEEDED", f"{state} {err}".strip())
        if state != "SUCCEEDED":
            return

        meta = self._metadata(s3t, s3, bucket_arn)
        snap = meta.get("current-snapshot-id")
        self.observe("snapshot_id", snap)
        self.record("snapshot", isinstance(snap, int) and snap > 0, f"current-snapshot-id={snap}")
        if not snap:
            return

        athena = self.client("athena")
        athena.create_work_group(Name=WORKGROUP, Tags=[{"Key": k, "Value": v} for k, v in self.tags.items()],
                                 Configuration={"ResultConfiguration": {"OutputLocation":
                                     f"s3://{data_bucket}/{PREFIX}/athena/"},
                                     "EnforceWorkGroupConfiguration": True})
        self.track("athena workgroup", WORKGROUP,
                   lambda: athena.delete_work_group(WorkGroup=WORKGROUP, RecursiveDeleteOption=True))
        ctx = {"Catalog": f"s3tablescatalog/{self.bucket}", "Database": NAMESPACE}
        digests = []
        for leg in ("a", "b"):
            dest = f"s3://{data_bucket}/{PREFIX}/unload-{leg}/"
            state, reason = _athena(athena, WORKGROUP, ctx, unload_sql(snap, dest))
            if state != "SUCCEEDED":
                self.record("unload_det", False, f"unload-{leg} {state}: {reason}")
                return
            digests.append(_content_digest(s3, data_bucket, f"{PREFIX}/unload-{leg}/"))
        self.observe("unload_digests", [d[0] for d in digests])
        self.record("unload_det", digests[0][0] == digests[1][0], f"sha256 a={digests[0][0][:16]} b={digests[1][0][:16]}")
        self.record("unload_count", digests[0][1] == ROWS, f"rows={digests[0][1]}")
        state, reason = _athena(athena, WORKGROUP, ctx,
                                unload_sql(snap, f"s3://{data_bucket}/{PREFIX}/unload-a/"))
        self.record("unload_nonempty", state == "FAILED", f"{state}: {reason}"[:200])

    # ── helpers ──────────────────────────────────────────────────────────────
    def _metadata(self, s3t, s3, bucket_arn) -> dict:
        loc = s3t.get_table_metadata_location(tableBucketARN=bucket_arn, namespace=NAMESPACE,
                                              name=TABLE)["metadataLocation"]
        b, _, k = loc[len("s3://"):].partition("/")
        return json.loads(s3.get_object(Bucket=b, Key=k)["Body"].read())

    def _glue_role(self, bucket_arn: str, data_bucket: str) -> str:
        iam = self.client("iam")
        trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
                 "Principal": {"Service": "glue.amazonaws.com"}, "Action": "sts:AssumeRole",
                 "Condition": {"StringEquals": {"aws:SourceAccount": self.account_id}}}]}
        arn = iam.create_role(RoleName=GLUE_ROLE, AssumeRolePolicyDocument=json.dumps(trust),
                              Tags=[{"Key": k, "Value": v} for k, v in self.tags.items()])["Role"]["Arn"]
        iam.put_role_policy(RoleName=GLUE_ROLE, PolicyName="spike",
                            PolicyDocument=json.dumps(glue_role_policy(
                                self.region, self.account_id, bucket_arn, data_bucket, self.bucket)))

        def _delete():
            iam.delete_role_policy(RoleName=GLUE_ROLE, PolicyName="spike")
            iam.delete_role(RoleName=GLUE_ROLE)
        self.track("iam role", GLUE_ROLE, _delete)
        time.sleep(12)  # IAM is eventually consistent; Glue rejects a role it cannot assume yet
        return arn


def glue_role_policy(region: str, account_id: str, bucket_arn: str, data_bucket: str,
                     bucket: str) -> dict:
    """Least-privilege IAM-only grants for the Glue writer. No Lake Formation, by design:
    whether IAM-only is enough is one of the questions this spike answers."""
    cat = f"arn:aws:glue:{region}:{account_id}"
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "S3TablesWrite", "Effect": "Allow",
         "Action": ["s3tables:GetTableBucket", "s3tables:GetNamespace", "s3tables:GetTable",
                    "s3tables:GetTableMetadataLocation", "s3tables:UpdateTableMetadataLocation",
                    "s3tables:GetTableData", "s3tables:PutTableData"],
         "Resource": [bucket_arn, f"{bucket_arn}/table/*"]},
        {"Sid": "GlueCatalogFederation", "Effect": "Allow",
         "Action": ["glue:GetCatalog", "glue:GetDatabase", "glue:GetTable", "glue:UpdateTable"],
         "Resource": [f"{cat}:catalog", f"{cat}:catalog/s3tablescatalog",
                      f"{cat}:catalog/s3tablescatalog/{bucket}",
                      f"{cat}:database/s3tablescatalog/{bucket}/{NAMESPACE}",
                      f"{cat}:table/s3tablescatalog/{bucket}/{NAMESPACE}/*"]},
        {"Sid": "ScriptAndTemp", "Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"],
         "Resource": f"arn:aws:s3:::{data_bucket}/{PREFIX}/*"},
        {"Sid": "Logs", "Effect": "Allow",
         "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{region}:{account_id}:log-group:/aws-glue/*"},
    ]}


def unload_sql(snapshot_id: int, dest: str) -> str:
    """The export statement Phase 1 will use. The snapshot must be an int: it is formatted into
    SQL, so anything else is refused rather than quoted."""
    if not isinstance(snapshot_id, int) or isinstance(snapshot_id, bool):
        raise TypeError("snapshot_id must be an int")
    if not dest.startswith("s3://") or "'" in dest:
        raise ValueError("dest must be an s3:// URI without quotes")
    return (f"UNLOAD (SELECT id, lang, payload FROM {TABLE} FOR VERSION AS OF {snapshot_id} "
            f"ORDER BY id) TO '{dest}' WITH (format = 'JSON')")


def content_digest(objects: list[bytes]) -> tuple[str, int]:
    """sha256 over the sorted decompressed lines of an UNLOAD output, plus the row count.
    Sorting makes the digest independent of file splits; decompressing makes it independent of
    gzip headers (which carry timestamps)."""
    lines = []
    for body in objects:
        raw = gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body
        lines += [ln for ln in raw.splitlines() if ln.strip()]
    lines.sort()
    return hashlib.sha256(b"\n".join(lines)).hexdigest(), len(lines)


def _content_digest(s3, bucket: str, prefix: str) -> tuple[str, int]:
    keys = [o["Key"] for p in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
            for o in p.get("Contents", [])]
    return content_digest([s3.get_object(Bucket=bucket, Key=k)["Body"].read() for k in keys])


def _delete_prefix(s3, bucket: str, prefix: str) -> None:
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix + "/"):
        objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if objs:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objs})


def _wait_glue(glue, job: str, run_id: str, timeout_s: int = 1800) -> tuple[str, str]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        jr = glue.get_job_run(JobName=job, RunId=run_id)["JobRun"]
        if jr["JobRunState"] in ("SUCCEEDED", "FAILED", "ERROR", "TIMEOUT", "STOPPED"):
            return jr["JobRunState"], jr.get("ErrorMessage", "")
        time.sleep(20)
    return "TIMEOUT", f"no terminal state in {timeout_s}s"


def _athena(athena, workgroup: str, ctx: dict, sql: str, timeout_s: int = 600) -> tuple[str, str]:
    qid = athena.start_query_execution(QueryString=sql, WorkGroup=workgroup,
                                       QueryExecutionContext=ctx)["QueryExecutionId"]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        st = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        if st["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            return st["State"], st.get("StateChangeReason", "")
        time.sleep(3)
    return "TIMEOUT", ""


if __name__ == "__main__":
    sys.exit(main(S7LakehouseSubstrate))
