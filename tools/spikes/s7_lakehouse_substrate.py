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
import _spike  # noqa: E402
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

#: --part domain (LLMOPS_S7_PART=domain). The account's Unified Studio domain is a shared,
#: pre-existing V2 domain holding other work (owner decision 2026-10-14: add one tagged spike
#: project, delete it afterwards, touch nothing else). Overridable for another account.
DOMAIN_NAME = "demo"
PROJECT_PROFILE = "Data analytics and AI-ML model development"
#: The read-only inline grant the spike adds to its own project's role (removed before the project).
PROJECT_GRANT = "llmops-spike-s7-catalog-read"
GRANT_SETTLE_S = 60
#: Services only the --part domain path calls, and why the build runner (tools/runner/) is not
#: granted them: creating a project in the shared domain needs a DataZone domain member, which is
#: the owner's identity, not the runner's role -- and a runner that could create projects in a
#: shared domain would reach well past this repo. This part runs from the owner's credentials.
#: tests/test_runner.py reads this instead of silently skipping the calls.
RUNNER_EXEMPT_SERVICES = {
    "datazone": "domain membership is the owner's, not the runner role's",
    "pricing": "read-only Price List lookup, only made by the laptop-run domain part",
}
#: OpenLineage on Glue 5 Spark, emitted to the domain's PostLineageEvent API. This is the exact
#: configuration the Unified Studio guide lists for Spark jobs; PR-07's jobs would carry it too.
LINEAGE_CONF = ("spark.extraListeners=io.openlineage.spark.agent.OpenLineageSparkListener"
                " --conf spark.openlineage.transport.type=amazon_datazone_api"
                " --conf spark.openlineage.transport.domainId={domain_id}"
                " --conf spark.glue.accountId={account_id}"
                " --conf spark.openlineage.columnLineage.datasetLineageEnabled=True"
                " --conf spark.glue.JOB_NAME={job}")

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
        ("smus_visible", "The table is visible as an asset in a Unified Studio project"),
        ("smus_lineage", "The Glue write's OpenLineage event reaches the domain and is processed"),
        ("smus_price", "The domain's price is read from the AWS Price List API"),
    )
    IMPLEMENTED = True  # substrate part by default; LLMOPS_S7_PART=domain runs the smus_* part

    def __init__(self, region, account_id=None, session=None, bucket=None, part=None):
        super().__init__(region, account_id, session)
        import os
        self.part = part or os.environ.get("LLMOPS_S7_PART", "substrate")
        if self.part not in ("substrate", "domain"):
            raise ValueError(f"unknown part {self.part!r}")
        # date-time split by a hyphen: a 14-digit stamp would be a digit run in the evidence
        self.bucket = bucket or f"{BUCKET_PREFIX}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"

    def plan(self) -> list[dict]:
        acct = self.account_id or "<ACCOUNT_ID>"
        if self.part == "domain":
            return [
                {"kind": "unified studio project", "name": f"llmops-spike-s7-* in domain {DOMAIN_NAME!r} "
                 f"(profile {PROJECT_PROFILE!r}: Tooling + Lakehouse Database + Redshift Serverless)"},
                {"kind": "s3tables bucket/namespace/table", "name": f"{self.bucket}/{NAMESPACE}/{TABLE}"},
                {"kind": "iam role", "name": f"arn:aws:iam::{acct}:role/{GLUE_ROLE} (+ datazone:PostLineageEvent)"},
                {"kind": "glue job", "name": f"{GLUE_JOB} (Glue 5.0, OpenLineage → the domain)"},
                {"kind": "inline policy", "name": f"{PROJECT_GRANT} on the spike project's own role (read-only catalog)"},
                {"kind": "datazone data source", "name": f"GLUE, catalog s3tablescatalog/{self.bucket}"},
            ]
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
        if self.part == "domain":
            return self._run_domain(data_bucket)
        s3t, s3 = self.client("s3tables"), self.client("s3")
        bucket_arn = self._spike_table(s3t, s3, data_bucket)

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
        state, err = self._glue_write(glue, s3, role_arn, data_bucket, catalog_id)
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

    # ── shared building blocks ───────────────────────────────────────────────
    def _spike_table(self, s3t, s3, data_bucket) -> str:
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
        return bucket_arn

    def _glue_write(self, glue, s3, role_arn, data_bucket, catalog_id, extra_args=None):
        key = f"{PREFIX}/glue_write.py"
        s3.put_object(Bucket=data_bucket, Key=key, Body=GLUE_SCRIPT.encode())
        glue.create_job(
            Name=GLUE_JOB, Role=role_arn, GlueVersion="5.0", WorkerType="G.1X", NumberOfWorkers=2,
            Command={"Name": "glueetl", "ScriptLocation": f"s3://{data_bucket}/{key}",
                     "PythonVersion": "3"},
            DefaultArguments={"--datalake-formats": "iceberg",
                              "--TempDir": f"s3://{data_bucket}/{PREFIX}/tmp/", **(extra_args or {})},
            Tags=self.tags)
        self.track("glue job", GLUE_JOB, lambda: glue.delete_job(JobName=GLUE_JOB))
        run_id = glue.start_job_run(JobName=GLUE_JOB, Arguments={
            "--catalog_id": catalog_id, "--rows": str(ROWS)})["JobRunId"]
        return _wait_glue(glue, GLUE_JOB, run_id)

    # ── --part domain: Unified Studio visibility, lineage, price ─────────────
    def _run_domain(self, data_bucket) -> None:
        dz, s3t = self.client("datazone"), self.client("s3tables")
        s3, glue = self.client("s3"), self.client("glue")
        domain = next((d for d in dz.list_domains()["items"] if d["name"] == DOMAIN_NAME), None)
        if domain is None:
            self.record("smus_visible", False, f"no domain named {DOMAIN_NAME!r}")
            return
        did = domain["id"]
        info = dz.get_domain(identifier=did)
        self.observe("domain", {"version": info.get("domainVersion"),
                                "single_sign_on": (info.get("singleSignOn") or {}).get("type")})
        profile = next(p for p in dz.list_project_profiles(domainIdentifier=did)["items"]
                       if p["name"] == PROJECT_PROFILE)
        self.record("smus_price", *self._price())

        started = time.time()
        kw = dict(domainIdentifier=did, name=f"llmops-spike-s7-{self.bucket[-15:]}",
                  description="spike S7 --part domain; deleted by the spike's teardown",
                  projectProfileId=profile["id"])
        try:
            pid = dz.create_project(**kw, resourceTags=dict(self.tags))["id"]
            self.observe("project_tags", "applied")
        except Exception as e:  # noqa: BLE001 -- only the one refusal is retried
            # Live, run 1: a profile with allowCustomProjectResourceTags=false refuses ANY extra
            # tag, so the spike's project cannot carry spike=; it is found by its name prefix.
            if "allowCustomProjectResourceTags" not in str(e):
                raise
            pid = dz.create_project(**kw)["id"]
            self.observe("project_tags", "refused by the profile (allowCustomProjectResourceTags=false); "
                                         "found by name prefix llmops-spike-s7-")
        self.track("unified studio project", pid, lambda: _delete_project(dz, did, pid))
        status, reasons = _wait_project(dz, did, pid)
        self.observe("project", {"status": status, "minutes": round((time.time() - started) / 60, 1),
                                 "failure_reasons": reasons})
        if status != "ACTIVE":
            self.record("smus_visible", False, f"project {status}: {reasons}")
            return
        # ACTIVE arrives before the environments finish deploying (run 2), and a data source needs
        # the Lakehouse environment (or its connection): wait for every ON_CREATE environment.
        envs = _wait_environments(dz, did, pid)
        self.observe("environments", {n: st for n, (st, _, _) in envs.items()})
        lake = next((v for n, v in envs.items() if "lakehouse" in n.lower()), None)
        if lake is None or lake[0] != "ACTIVE":
            why = "; ".join(f"{n}: {st} {msg}" for n, (st, _, msg) in envs.items() if st != "ACTIVE")
            self.record("smus_visible", False, f"no ACTIVE Lakehouse environment: {why}"[:300])
            return

        bucket_arn = self._spike_table(s3t, s3, data_bucket)
        catalog_id = f"{self.account_id}:s3tablescatalog/{self.bucket}"
        role_arn = self._glue_role(bucket_arn, data_bucket, extra=[{
            "Sid": "OpenLineageToTheDomain", "Effect": "Allow", "Action": "datazone:PostLineageEvent",
            "Resource": f"arn:aws:datazone:{self.region}:{self.account_id}:domain/{did}"}])
        job_started = _dt_now()
        state, err = self._glue_write(glue, s3, role_arn, data_bucket, catalog_id, extra_args={
            "--conf": LINEAGE_CONF.format(domain_id=did, account_id=self.account_id, job=GLUE_JOB)})
        self.observe("glue_write", f"{state} {err}".strip())

        # The project role the blueprint creates cannot read an S3 Tables catalog (run 5:
        # "Not able to access Glue Catalog ... Project Role has IAM or Lake Formation
        # permissions"), and a connection-based data source refuses its own dataAccessRole
        # (run 4). So the project role is granted read on this one catalog, and the grant is
        # removed BEFORE the project: CloudFormation cannot delete a role carrying an inline
        # policy it did not create.
        project_role = _project_role(dz, did, envs)
        if not project_role:
            self.record("smus_visible", False, "no project role in the Tooling environment's provisioned resources")
            return
        self._grant_project_role(project_role)
        # A V2 domain refuses environmentIdentifier on a data source (run 3); it takes the
        # project's connection, whose name and type are recorded because the docs do not say
        # which one a Glue source over s3tablescatalog wants.
        conns = dz.list_connections(domainIdentifier=did, projectIdentifier=pid).get("items", [])
        self.observe("connections", [(c.get("name"), c.get("type")) for c in conns])
        conn = pick_connection(conns)
        if conn is None:
            self.record("smus_visible", False, "the project has no connection a Glue data source can use")
            return
        attempts, ds, ready = [], None, ""
        for attempt in range(2):  # a second attempt only after a FAILED creation, a minute later
            ds = dz.create_data_source(
                domainIdentifier=did, projectIdentifier=pid,
                name=f"llmops-spike-s7-{self.bucket[-15:]}-{attempt}",
                connectionIdentifier=conn["connectionId"], type="GLUE", publishOnImport=False,
                enableSetting="ENABLED", configuration={"glueRunConfiguration": {
                    "catalogName": f"s3tablescatalog/{self.bucket}",
                    "relationalFilterConfigurations": [{"databaseName": NAMESPACE}]}})["id"]
            self.track("data source", ds, lambda ds=ds: dz.delete_data_source(domainIdentifier=did, identifier=ds))
            # Creation is asynchronous: StartDataSourceRun on a data source that is not READY is
            # refused (run 4).
            ready = _wait_ds_ready(dz, did, ds)
            attempts.append(ready[:160])
            if ready == "READY":
                break
            time.sleep(GRANT_SETTLE_S)
        self.observe("data_source_attempts", attempts)
        self.observe("data_source_status", ready)
        if ready != "READY":
            self.record("smus_visible", False, f"data source {ready}")
            return
        run = dz.start_data_source_run(domainIdentifier=did, dataSourceIdentifier=ds)["id"]
        run_status = _wait_ds_run(dz, did, run)
        hits = [i for i in _search_assets(dz, did, pid, TABLE)]
        self.observe("data_source_run", run_status)
        self.record("smus_visible", bool(hits), f"run {run_status}; {len(hits)} asset(s) named {TABLE!r}"
                    if hits else f"run {run_status}; no asset named {TABLE!r} in the project")

        found = _lineage_events(dz, did, job_started, (GLUE_JOB, TABLE))
        self.observe("lineage_events", found)
        ok = [e for e in found if e["status"] == "SUCCESS" and e["mentions_table"]]
        self.record("smus_lineage", bool(ok), f"{len(found)} event(s) since the write, {len(ok)} "
                    f"processed and naming {TABLE!r}")

    def _grant_project_role(self, role_arn: str) -> None:
        iam, name = self.client("iam"), role_arn.rsplit("/", 1)[-1]
        iam.put_role_policy(RoleName=name, PolicyName=PROJECT_GRANT, PolicyDocument=json.dumps(
            datasource_role_policy(self.region, self.account_id, self.bucket)))
        self.track("project role grant", f"{name}/{PROJECT_GRANT}",
                   lambda: iam.delete_role_policy(RoleName=name, PolicyName=PROJECT_GRANT))
        # Run 6: the data source's GetCatalog as this role came 15 s after the grant and was
        # denied. Propagation is the hypothesis being tested, so wait well past it.
        time.sleep(GRANT_SETTLE_S)

    def _price(self) -> tuple[bool, str]:
        pricing = self.client("pricing")  # the Price List API itself is served from us-east-1
        dims, token = [], None
        while True:
            # DataZone's prices are global -- live, regionCode is "" and location "Any" -- so a
            # regionCode filter on the Region returns nothing (runs 1-2): fetch all, keep this
            # Region's and the global ones.
            page = pricing.get_products(ServiceCode="AmazonDataZone", MaxResults=100,
                                        **({"NextToken": token} if token else {}))
            for raw in page.get("PriceList", []):
                prod = json.loads(raw)
                attrs = prod.get("product", {}).get("attributes", {})
                if attrs.get("regionCode") != self.region and attrs.get("location") != "Any":
                    continue
                for term in prod.get("terms", {}).get("OnDemand", {}).values():
                    for d in term.get("priceDimensions", {}).values():
                        usd = d.get("pricePerUnit", {}).get("USD")
                        if usd is not None:
                            dims.append({"description": d.get("description", "")[:90],
                                         "unit": d.get("unit"), "usd": usd})
            token = page.get("NextToken")
            if not token:
                break
        self.observe("smus_price_dimensions", dims[:12])
        return bool(dims), f"{len(dims)} on-demand price dimension(s) for AmazonDataZone ({self.region} or global)"

    # ── helpers ──────────────────────────────────────────────────────────────
    def _metadata(self, s3t, s3, bucket_arn) -> dict:
        loc = s3t.get_table_metadata_location(tableBucketARN=bucket_arn, namespace=NAMESPACE,
                                              name=TABLE)["metadataLocation"]
        b, _, k = loc[len("s3://"):].partition("/")
        return json.loads(s3.get_object(Bucket=b, Key=k)["Body"].read())

    def _glue_role(self, bucket_arn: str, data_bucket: str, extra: list | None = None) -> str:
        iam = self.client("iam")
        trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
                 "Principal": {"Service": "glue.amazonaws.com"}, "Action": "sts:AssumeRole",
                 "Condition": {"StringEquals": {"aws:SourceAccount": self.account_id}}}]}
        arn = iam.create_role(RoleName=GLUE_ROLE, AssumeRolePolicyDocument=json.dumps(trust),
                              **_spike.boundary_kwargs(), Tags=[{"Key": k, "Value": v} for k, v in self.tags.items()])["Role"]["Arn"]
        iam.put_role_policy(RoleName=GLUE_ROLE, PolicyName="spike",
                            PolicyDocument=json.dumps(glue_role_policy(
                                self.region, self.account_id, bucket_arn, data_bucket, self.bucket,
                                extra)))

        def _delete():
            iam.delete_role_policy(RoleName=GLUE_ROLE, PolicyName="spike")
            iam.delete_role(RoleName=GLUE_ROLE)
        self.track("iam role", GLUE_ROLE, _delete)
        time.sleep(12)  # IAM is eventually consistent; Glue rejects a role it cannot assume yet
        return arn


def glue_role_policy(region: str, account_id: str, bucket_arn: str, data_bucket: str,
                     bucket: str, extra: list | None = None) -> dict:
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
    ] + list(extra or [])}


def datasource_role_policy(region: str, account_id: str, bucket: str) -> dict:
    """Read-only catalog access for the DataZone Glue data source: the one spike table bucket's
    federated catalog entries, nothing writable."""
    cat = f"arn:aws:glue:{region}:{account_id}"
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "ReadTheSpikeCatalog", "Effect": "Allow",
         "Action": ["glue:GetCatalog", "glue:GetCatalogs", "glue:GetDatabase", "glue:GetDatabases",
                    "glue:GetTable", "glue:GetTables", "glue:GetPartitions"],
         "Resource": [f"{cat}:catalog", f"{cat}:catalog/s3tablescatalog",
                      f"{cat}:catalog/s3tablescatalog/{bucket}",
                      f"{cat}:database/s3tablescatalog/{bucket}/{NAMESPACE}",
                      f"{cat}:table/s3tablescatalog/{bucket}/{NAMESPACE}/*"]},
    ]}


def _dt_now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc)


def _wait_project(dz, did, pid, timeout_s=2700) -> tuple[str, list]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        p = dz.get_project(domainIdentifier=did, identifier=pid)
        status = p.get("projectStatus", "")
        if status in ("ACTIVE", "FAILED", "UPDATE_FAILED", "DELETE_FAILED", "MOVING"):
            return status, [r.get("message", "")[:200] for r in p.get("failureReasons", [])]
        time.sleep(30)
    return "TIMEOUT", []


def _project_role(dz, did, envs) -> str | None:
    """The project role ARN, from the Tooling environment's provisioned resources."""
    tooling = next((v for n, v in envs.items() if n.lower() == "tooling"), None)
    if tooling is None:
        return None
    env = dz.get_environment(domainIdentifier=did, identifier=tooling[1])
    for r in env.get("provisionedResources", []):
        if r.get("name") in ("userRoleArn", "projectRoleArn") or (
                r.get("type") == "IAMRole" and ":role/" in r.get("value", "")):
            return r["value"]
    return None


def pick_connection(conns: list[dict]) -> dict | None:
    """The connection a Glue data source over the lakehouse should use: one named for the
    lakehouse first, then the project's IAM connection, then its Athena one."""
    for test in (lambda c: "lakehouse" in (c.get("name") or "").lower(),
                 lambda c: c.get("type") == "IAM", lambda c: c.get("type") == "ATHENA"):
        hit = next((c for c in conns if test(c)), None)
        if hit:
            return hit
    return None


def _wait_environments(dz, did, pid, timeout_s=2700, settle_s=120) -> dict:
    """{name: (status, id, failure message)} once no environment is still in progress. Waits
    `settle_s` first: environments with a later deploymentOrder appear only after earlier ones."""
    time.sleep(settle_s)
    deadline, busy = time.time() + timeout_s, ("CREATING", "UPDATING", "VALIDATION_IN_PROGRESS", "PENDING")
    while time.time() < deadline:
        out = {}
        for e in dz.list_environments(domainIdentifier=did, projectIdentifier=pid).get("items", []):
            g = dz.get_environment(domainIdentifier=did, identifier=e["id"])
            msg = ((g.get("lastDeployment") or {}).get("failureReason") or {}).get("message", "")
            out[e["name"]] = (g.get("status", ""), e["id"], msg[:200])
        if out and not any(st in busy for st, _, _ in out.values()):
            return out
        time.sleep(30)
    return out


def _delete_project(dz, did, pid, timeout_s=2700) -> None:
    dz.delete_project(domainIdentifier=did, identifier=pid, skipDeletionCheck=True)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            st = dz.get_project(domainIdentifier=did, identifier=pid).get("projectStatus", "")
        except dz.exceptions.ResourceNotFoundException:
            return
        if st == "DELETE_FAILED":
            raise RuntimeError(f"project {pid} DELETE_FAILED")
        time.sleep(30)
    raise RuntimeError(f"project {pid} not deleted in {timeout_s}s")


def _wait_ds_ready(dz, did, ds_id, timeout_s=600) -> str:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        d = dz.get_data_source(domainIdentifier=did, identifier=ds_id)
        st = d.get("status", "")
        if st == "READY" or st.startswith("FAILED"):
            err = (d.get("errorMessage") or {}).get("errorDetail", "")
            return st + (f": {err[:200]}" if err else "")
        time.sleep(10)
    return "TIMEOUT"


def _wait_ds_run(dz, did, run_id, timeout_s=1200) -> str:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = dz.get_data_source_run(domainIdentifier=did, identifier=run_id)
        if r["status"] in ("SUCCESS", "FAILED", "PARTIALLY_SUCCEEDED"):
            err = (r.get("errorMessage") or {}).get("errorDetail", "")
            return r["status"] + (f": {err[:200]}" if err else "")
        time.sleep(15)
    return "TIMEOUT"


def _search_assets(dz, did, pid, name) -> list:
    out = dz.search(domainIdentifier=did, owningProjectIdentifier=pid, searchScope="ASSET",
                    searchText=name, maxResults=20).get("items", [])
    return [i for i in out if (i.get("assetItem") or {}).get("name") == name]


def lineage_mentions(event_text: str, names) -> dict:
    """Which of `names` an OpenLineage event's JSON mentions (job name, table name)."""
    return {n: n in event_text for n in names}


def _lineage_events(dz, did, after, names, wait_s=600) -> list:
    """Events posted since `after`, polled for up to `wait_s`: lineage is processed asynchronously."""
    deadline, out = time.time() + wait_s, []
    while time.time() < deadline:
        items = dz.list_lineage_events(domainIdentifier=did, timestampAfter=after).get("items", [])
        out = []
        for it in items:
            ev = dz.get_lineage_event(domainIdentifier=did, identifier=it["id"])
            body = ev["event"].read().decode() if hasattr(ev["event"], "read") else str(ev["event"])
            m = lineage_mentions(body, names)
            out.append({"status": it.get("processingStatus"), "mentions_job": m[names[0]],
                        "mentions_table": m[names[1]]})
        if any(e["status"] == "SUCCESS" and e["mentions_table"] for e in out):
            return out
        time.sleep(30)
    return out


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
