"""deploy/10_lakehouse.py, offline: the table specs, the key and role policies, and the
deploy/teardown symmetry, against an in-memory fake of the AWS clients.

The live proof is deploy/evidence/VERIFICATION_lakehouse_storage.md (`--verify`: 34 checks,
including simulate_principal_policy on the Glue role in both directions). These tests pin the
properties that make that proof repeatable: every table is one Glue/Athena can see, the keys
cannot be used by anyone the policy does not name, the role holds nothing it does not need, a
second deploy creates nothing, and teardown removes everything deploy creates.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lakehouse", REPO / "deploy" / "10_lakehouse.py")
lh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lh)

ACCOUNT, REGION, DATA = "123456789012", "us-east-1", "data-bucket"  # documentation account


# ── specs ────────────────────────────────────────────────────────────────────
def test_every_table_spec_is_valid():
    lh.validate_tables()
    assert {t["namespace"] for t in lh.TABLES} == {"bronze", "silver", "gold"}


@pytest.mark.parametrize("mutate,needle", [
    (lambda t: t.update(name="Conversation_Events"), "lowercase"),
    (lambda t: t["fields"][0].update(type="varchar"), "unknown Iceberg type"),
    (lambda t: t.update(partition=[("nope", "day", "d")]), "not a column"),
    (lambda t: t.update(partition=[("ingest_ts", "bucket", "b")]), "unsupported transform"),
    (lambda t: t["fields"][1].update(name=t["fields"][0]["name"]), "duplicate field"),
    (lambda t: t.update(namespace="gold"), "is not in"),
])
def test_the_validator_rejects_what_glue_or_athena_would_hide(mutate, needle):
    tables = copy.deepcopy(lh.TABLES)
    mutate(tables[0])
    with pytest.raises(ValueError, match=needle):
        lh.validate_tables(tables)


def test_partition_specs_reference_their_source_columns_by_id():
    t = next(t for t in lh.TABLES if t["name"] == "conversation_events")
    meta = lh.table_metadata(t)["iceberg"]
    ids = {f["name"]: f["id"] for f in meta["schema"]["fields"]}
    (p,) = meta["partitionSpec"]["fields"]
    assert p["sourceId"] == ids["ingest_ts"] and p["transform"] == "day" and p["fieldId"] >= 1000
    gold = next(t for t in lh.TABLES if t["name"] == "qlora_train")
    assert "partitionSpec" not in lh.table_metadata(gold)["iceberg"]


def test_the_gold_contract_columns_match_the_research_report():
    """§2: instruction, input, output + split + pair_id; the trainer maps them to messages."""
    for name in ("qlora_train", "qlora_eval"):
        t = next(t for t in lh.TABLES if t["name"] == name)
        assert {f["name"] for f in t["fields"]} == {"pair_id", "instruction", "input", "output", "split"}
    m = next(t for t in lh.TABLES if t["name"] == "dataset_manifest")
    assert {"snapshot_id", "sha256", "export_uri", "row_count"} <= {f["name"] for f in m["fields"]}


# ── policies ─────────────────────────────────────────────────────────────────
def test_each_key_lets_s3_tables_maintenance_use_it_for_its_own_bucket_only():
    for b in (lh.RAW, lh.CURATED):
        pol = lh.key_policy(REGION, ACCOUNT, b)
        principals = [json.dumps(st["Principal"]) for st in pol["Statement"]]
        assert len(principals) == 2, "only the account and the maintenance principal"
        maint = next(st for st in pol["Statement"] if "Service" in st["Principal"])
        assert maint["Principal"]["Service"] == "maintenance.s3tables.amazonaws.com"
        assert sorted(maint["Action"]) == ["kms:Decrypt", "kms:GenerateDataKey"]
        ctx = maint["Condition"]["StringLike"]["kms:EncryptionContext:aws:s3:arn"]
        assert ctx == f"{lh.bucket_arn(REGION, ACCOUNT, b)}/*"


def test_the_glue_role_holds_no_wildcard_and_no_destructive_action():
    keys = {lh.RAW: "arn:aws:kms:us-east-1:1:key/raw", lh.CURATED: "arn:aws:kms:us-east-1:1:key/cur"}
    pol = lh.glue_role_policy(REGION, ACCOUNT, DATA, keys)
    actions = [a for st in pol["Statement"] for a in (st["Action"] if isinstance(st["Action"], list) else [st["Action"]])]
    resources = [r for st in pol["Statement"] for r in (st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]])]
    assert "*" not in resources and not any(a.endswith(":*") for a in actions)
    destructive = ("Delete", "Create", "Schedule", "Disable")
    # The data and key services only: a job creating its own log stream is not destructive.
    data = [a for a in actions if a.split(":")[0] in ("s3tables", "glue", "kms", "s3")]
    assert not [a for a in data if a.split(":")[1].startswith(destructive)], data
    kms = next(st for st in pol["Statement"] if st["Sid"] == "LakehouseKeys")
    assert kms["Resource"] == [keys[lh.RAW], keys[lh.CURATED]]
    s3 = next(st for st in pol["Statement"] if st["Sid"] == "ScriptsAndTemp")
    assert s3["Resource"] == f"arn:aws:s3:::{DATA}/{lh.GLUE_PREFIX}/*"


def test_the_live_role_check_asks_both_directions():
    arns = {b: lh.bucket_arn(REGION, ACCOUNT, b) for b in (lh.RAW, lh.CURATED)}
    cases = lh.glue_role_cases(REGION, ACCOUNT, DATA, arns, {lh.RAW: "k1", lh.CURATED: "k2"})
    assert {want for _, _, want in cases} == {True, False}
    assert sum(1 for *_, w in cases if not w) >= 5, "the denials are the half that proves scoping"


def test_the_workgroup_enforces_its_result_location_and_leaves_encryption_to_the_bucket():
    """An explicit result encryption makes every Athena INSERT into a CMK table fail (PR-08), so
    the workgroup sets none and the data bucket's default encrypts results and extracts."""
    cfg = lh.workgroup_config(DATA)
    assert cfg["EnforceWorkGroupConfiguration"] is True
    assert cfg["ResultConfiguration"] == {"OutputLocation": f"s3://{DATA}/{lh.ATHENA_PREFIX}/"}


class _Athena:
    class exceptions:
        InvalidRequestException = type("InvalidRequestException", (Exception,), {})

    def __init__(self, cfg):
        self.cfg, self.updates = cfg, []

    def get_work_group(self, WorkGroup):
        return {"WorkGroup": {"Configuration": self.cfg}}

    def update_work_group(self, WorkGroup, ConfigurationUpdates):
        self.updates.append(ConfigurationUpdates)


def test_an_existing_workgroup_with_the_override_is_reconciled_not_reported_as_fine():
    a = _Athena({"ResultConfiguration": {"OutputLocation": "s3://d/x/",
                                         "EncryptionConfiguration": {"EncryptionOption": "SSE_S3"}}})
    assert lh.ensure_workgroup(a, DATA).startswith("updated")
    assert a.updates == [{"ResultConfigurationUpdates": {"RemoveEncryptionConfiguration": True}}]
    a = _Athena(lh.workgroup_config(DATA))
    assert lh.ensure_workgroup(a, DATA) == "exists" and a.updates == []


def test_raw_retention_is_short_and_curated_is_left_at_the_defaults():
    assert lh.RAW_SNAPSHOT["maxSnapshotAgeHours"] <= 72
    assert lh.RAW_FILE_REMOVAL == {"unreferencedDays": 1, "nonCurrentDays": 1}


# ── deploy / teardown against a fake account ─────────────────────────────────
class _NotFound(Exception):
    pass


class FakeAccount:
    """Enough of KMS, S3 Tables, Athena, IAM, SSM and Glue for deploy() and teardown()."""

    def __init__(self, integration=True):
        self.calls, self.integration = [], integration
        self.keys, self.aliases, self.buckets, self.ns, self.tables = {}, {}, {}, set(), set()
        self.workgroup, self.roles = False, {}  # role name -> its inline policies
        self.params, self.guardrails, self.jobs, self.objects = {}, {}, {}, {}

    def client(self, service, region_name=None):
        return _Client(self, service)


class _Client:
    def __init__(self, acct, service):
        self.a, self.svc = acct, service
        self.exceptions = type("E", (), {n: _NotFound for n in (
            "NotFoundException", "EntityNotFoundException", "InvalidRequestException",
            "NoSuchEntityException", "ParameterNotFound")})

    def get_paginator(self, op):
        a = self.a
        return type("P", (), {"paginate": lambda _s, **kw: [{"Aliases": [
            {"AliasName": k, "TargetKeyId": v} for k, v in a.aliases.items()]}]})()

    def __getattr__(self, op):
        def call(**kw):
            self.a.calls.append((self.svc, op))
            fn = type(self).__dict__.get(f"_{self.svc}_{op}")
            return fn(self, **kw) if fn else {}
        return call

    # glue
    def _glue_get_catalog(self, **_):
        if not self.a.integration:
            raise _NotFound()
        return {}

    # kms
    def _kms_create_key(self, **kw):
        arn = f"arn:aws:kms:{REGION}:{ACCOUNT}:key/k{len(self.a.keys)}"
        self.a.keys[arn] = json.loads(kw["Policy"])
        return {"KeyMetadata": {"Arn": arn, "KeyId": arn.rsplit("/", 1)[1]}}

    def _kms_create_alias(self, **kw):
        self.a.aliases[kw["AliasName"]] = kw["TargetKeyId"]

    def _kms_describe_key(self, **kw):
        k = self.a.aliases.get(kw["KeyId"], kw["KeyId"])
        if k not in self.a.keys:
            raise _NotFound()
        return {"KeyMetadata": {"Arn": k, "KeyId": k.rsplit("/", 1)[1]}}

    def _kms_delete_alias(self, **kw):
        self.a.aliases.pop(kw["AliasName"])

    # s3tables
    def _s3tables_list_table_buckets(self, **kw):
        return {"tableBuckets": [{"name": n, "arn": a} for n, a in self.a.buckets.items()
                                 if n.startswith(kw.get("prefix", ""))]}

    def _s3tables_create_table_bucket(self, **kw):
        assert kw["encryptionConfiguration"]["sseAlgorithm"] == "aws:kms"
        arn = lh.bucket_arn(REGION, ACCOUNT, kw["name"])
        self.a.buckets[kw["name"]] = arn
        return {"arn": arn}

    def _s3tables_delete_table_bucket(self, **kw):
        assert not [t for t in self.a.tables if t[0] == kw["tableBucketARN"]], "tables still exist"
        self.a.buckets = {n: a for n, a in self.a.buckets.items() if a != kw["tableBucketARN"]}

    def _s3tables_get_namespace(self, **kw):
        if (kw["tableBucketARN"], kw["namespace"]) not in self.a.ns:
            raise _NotFound()

    def _s3tables_create_namespace(self, **kw):
        self.a.ns.add((kw["tableBucketARN"], kw["namespace"][0]))

    def _s3tables_delete_namespace(self, **kw):
        self.a.ns.discard((kw["tableBucketARN"], kw["namespace"]))

    def _s3tables_get_table(self, **kw):
        if (kw["tableBucketARN"], kw["namespace"], kw["name"]) not in self.a.tables:
            raise _NotFound()

    def _s3tables_create_table(self, **kw):
        assert kw["format"] == "ICEBERG" and "tags" not in kw, "§4.3: no table tags"
        self.a.tables.add((kw["tableBucketARN"], kw["namespace"], kw["name"]))

    def _s3tables_delete_table(self, **kw):
        self.a.tables.discard((kw["tableBucketARN"], kw["namespace"], kw["name"]))

    # athena
    def _athena_get_work_group(self, **_):
        if not self.a.workgroup:
            raise _NotFound()
        return {"WorkGroup": {"Configuration": self.a.workgroup}}

    def _athena_create_work_group(self, Configuration, **_):
        self.a.workgroup = Configuration

    def _athena_delete_work_group(self, **_):
        if not self.a.workgroup:
            raise _NotFound()
        self.a.workgroup = False

    # iam
    def _iam_get_role(self, RoleName, **_):
        if RoleName not in self.a.roles:
            raise _NotFound()
        return {"Role": {"Arn": f"arn:aws:iam::{ACCOUNT}:role/{RoleName}"}}

    def _iam_create_role(self, RoleName, **_):
        self.a.roles[RoleName] = {}
        return {"Role": {"Arn": f"arn:aws:iam::{ACCOUNT}:role/{RoleName}"}}

    def _iam_put_role_policy(self, RoleName, PolicyName, PolicyDocument, **_):
        self.a.roles[RoleName][PolicyName] = PolicyDocument

    def _iam_delete_role_policy(self, RoleName, PolicyName, **_):
        if PolicyName not in self.a.roles.get(RoleName, {}):
            raise _NotFound()
        del self.a.roles[RoleName][PolicyName]

    def _iam_delete_role(self, RoleName, **_):
        if RoleName not in self.a.roles:
            raise _NotFound()
        assert not self.a.roles[RoleName], "IAM refuses to delete a role that still has policies"
        del self.a.roles[RoleName]

    # bedrock guardrails
    def _bedrock_list_guardrails(self, **kw):
        if "guardrailIdentifier" in kw:
            g = self.a.guardrails[kw["guardrailIdentifier"]]
            return {"guardrails": [{"version": v} for v in ["DRAFT", *g["versions"]]]}
        return {"guardrails": [{"id": i, "name": g["name"], "arn": f"arn:g/{i}"} for i, g in self.a.guardrails.items()]}

    def _bedrock_create_guardrail(self, **kw):
        pii = kw["sensitiveInformationPolicyConfig"]["piiEntitiesConfig"]
        assert all(e["inputEnabled"] and e["outputEnabled"] for e in pii), "S4: enable both directions"
        gid = f"gr{len(self.a.guardrails)}"
        self.a.guardrails[gid] = {"name": kw["name"], "versions": []}
        return {"guardrailId": gid, "guardrailArn": f"arn:g/{gid}"}

    def _bedrock_create_guardrail_version(self, **kw):
        g = self.a.guardrails[kw["guardrailIdentifier"]]
        g["versions"].append(str(len(g["versions"]) + 1))
        return {"version": g["versions"][-1]}

    def _bedrock_delete_guardrail(self, **kw):
        self.a.guardrails.pop(kw["guardrailIdentifier"])

    # glue jobs and s3 scripts
    def _glue_get_job(self, **kw):
        if kw["JobName"] not in self.a.jobs:
            raise _NotFound()
        return {"Job": self.a.jobs[kw["JobName"]]}

    def _glue_create_job(self, **kw):
        self.a.jobs[kw["Name"]] = kw

    def _glue_update_job(self, **kw):
        self.a.jobs[kw["JobName"]].update(kw["JobUpdate"])

    def _glue_delete_job(self, **kw):
        self.a.jobs.pop(kw["JobName"], None)

    def _s3_put_object(self, **kw):
        self.a.objects[kw["Key"]] = kw["Body"]

    # ssm
    def _ssm_put_parameter(self, **kw):
        self.a.params[kw["Name"]] = kw["Value"]

    def _ssm_delete_parameter(self, **kw):
        if self.a.params.pop(kw["Name"], None) is None:
            raise _NotFound()


def test_deploy_creates_everything_and_publishes_its_outputs():
    acct = FakeAccount()
    out = lh.deploy(acct, REGION, ACCOUNT, DATA)
    assert set(acct.buckets) == {lh.RAW, lh.CURATED}
    assert len(acct.tables) == len(lh.TABLES) and len(acct.keys) == 2
    assert set(acct.params) == {f"{lh.SSM_PREFIX}/{k}" for k in lh.SSM_KEYS}
    assert all(v != "exists" for k, v in out.items() if k.startswith(("table", "bucket", "key")))
    assert ("kms", "enable_key_rotation") in acct.calls
    raw_tables = [t for t in lh.TABLES if t["bucket"] == lh.RAW]
    assert acct.calls.count(("s3tables", "put_table_maintenance_configuration")) == len(raw_tables)


def test_a_second_deploy_creates_nothing():
    acct = FakeAccount()
    lh.deploy(acct, REGION, ACCOUNT, DATA)
    before = len([c for c in acct.calls if c[1].startswith("create")])
    out = lh.deploy(acct, REGION, ACCOUNT, DATA)
    assert len([c for c in acct.calls if c[1].startswith("create")]) == before
    assert all(v == "exists" for k, v in out.items() if k.startswith(("table", "bucket", "key", "namespace")))


def test_teardown_removes_everything_deploy_created_and_only_schedules_the_keys():
    acct = FakeAccount()
    lh.deploy(acct, REGION, ACCOUNT, DATA)
    out = lh.teardown(acct, REGION, ACCOUNT)
    assert not acct.buckets and not acct.tables and not acct.ns and not acct.params
    assert not acct.workgroup and not acct.roles and not acct.aliases
    assert not acct.jobs and not acct.guardrails
    assert acct.calls.count(("kms", "schedule_key_deletion")) == 2
    assert all("30 days" in out[f"key {b}"] for b in (lh.RAW, lh.CURATED))
    assert ("kms", "delete_key") not in acct.calls


def test_teardown_of_nothing_is_harmless():
    out = lh.teardown(FakeAccount(), REGION, ACCOUNT)
    assert set(out.values()) == {"absent"}


def test_deploy_refuses_to_enable_the_account_level_integration():
    acct = FakeAccount(integration=False)
    with pytest.raises(SystemExit, match="does not enable it"):
        lh.deploy(acct, REGION, ACCOUNT, DATA)
    assert not [c for c in acct.calls if c[1].startswith(("create", "put"))]


def test_the_offline_dry_run_makes_no_aws_call(capsys, monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "boto3", None)  # any import would fail
    assert lh.main(["--region", REGION, "--dry-run", "--account-id", ACCOUNT, "--bucket", DATA]) == 0
    printed = capsys.readouterr().out
    assert printed.count("would ensure table:") == len(lh.TABLES)



def test_deploy_creates_a_numbered_guardrail_version_and_every_job():
    acct = FakeAccount()
    lh.deploy(acct, REGION, ACCOUNT, DATA)
    (g,) = acct.guardrails.values()
    assert g["versions"] == ["1"] and acct.params[f"{lh.SSM_PREFIX}/guardrail_version"] == "1"
    assert set(acct.jobs) == {n for n, _ in lh.JOBS.values()}
    for name, script in lh.JOBS.values():
        assert f"{lh.GLUE_PREFIX}/{script}" in acct.objects
        args = acct.jobs[name]["DefaultArguments"]
        shipped = lh.SHELL_JOB_MODULE.get(script, "transform.py")
        assert args["--extra-py-files"].endswith(f"{lh.GLUE_PREFIX}/{shipped}"), (script, args)
        assert f"{lh.GLUE_PREFIX}/{shipped}" in acct.objects, shipped
        static = lh.static_arguments(script, ACCOUNT, DATA, "gr0", "1")
        assert {k: args[k] for k in static} == static, f"{script}: its run-invariant arguments must be defaults"
    roles = {acct.jobs[n]["Role"].rsplit("/", 1)[-1] for n, s in lh.JOBS.values() if s != "generate_landing.py"}
    assert roles == {lh.GLUE_ROLE}
    assert acct.jobs[lh.JOBS["generate"][0]]["Role"].endswith(f"/{lh.GENERATOR_ROLE}")
    assert set(acct.roles) == {lh.GLUE_ROLE, lh.GENERATOR_ROLE}
    assert acct.objects[f"{lh.GLUE_PREFIX}/transform.py"] == lh.TRANSFORM_SRC.read_bytes()
    lh.deploy(acct, REGION, ACCOUNT, DATA)
    assert g["versions"] == ["1"], "a second deploy must reuse the published version"


def test_the_job_grants_are_scoped_to_their_prefixes_and_the_one_guardrail():
    keys = {lh.RAW: "k1", lh.CURATED: "k2"}
    pol = lh.glue_role_policy(REGION, ACCOUNT, DATA, keys, "arn:aws:bedrock:us-east-1:1:guardrail/g")
    by = {st["Sid"]: st for st in pol["Statement"]}
    assert by["NamesByGuardrail"]["Resource"] == "arn:aws:bedrock:us-east-1:1:guardrail/g"
    assert by["LandingRead"]["Action"] == "s3:GetObject" and by["ReportsWrite"]["Action"] == "s3:PutObject"
    assert by["LandingRead"]["Resource"].endswith(f"/{lh.LANDING_PREFIX}/*")
    assert by["ReportsWrite"]["Resource"].endswith(f"/{lh.REPORT_PREFIX}/*")
    assert "NamesByGuardrail" not in {st["Sid"] for st in lh.glue_role_policy(REGION, ACCOUNT, DATA, keys)["Statement"]}


def test_every_job_script_exists_and_imports_transform():
    for _, script in lh.JOBS.values():
        src = (lh.GLUE_SRC / script).read_text()
        assert "getResolvedOptions" in src
        if script in lh.PYTHON_SHELL_JOBS:
            assert f"import {lh.SHELL_JOB_MODULE[script][:-3]}" in src, script
        elif script != "land_bronze.py":
            assert "import transform" in src, script


def test_the_guardrail_job_installs_an_sdk_that_has_apply_guardrail():
    d = lh.job_definition("j", "bronze_to_silver.py", "r", DATA)["DefaultArguments"]
    mods = dict(m.split("==") for m in d["--additional-python-modules"].split(","))
    assert tuple(map(int, mods["boto3"].split("."))) >= (1, 43, 51), "AGENTS.md SDK gate"
    assert "--additional-python-modules" not in lh.job_definition("j", "land_bronze.py", "r", DATA)["DefaultArguments"]


def test_every_job_report_carries_rows_and_snapshots_for_the_data_tab():
    """The console's Data tab reads the reports, never the tables: each job must say what it
    wrote (rows, current snapshot) or the tab has nothing honest to show."""
    for _, script in lh.JOBS.values():
        if script == "generate_landing.py":
            continue  # writes source files, not tables: its record is landing/<dataset>/manifest.json
        src = (lh.GLUE_SRC / script).read_text()
        assert '["tables"]' in src and "snapshots" in src and "report_uri" in src, script
    assert lh.run_arguments("land", "b", "d")["--report_uri"] == "s3://b/lakehouse/reports/d/land.json"
    assert lh.run_arguments("silver", "b", "d")["--expected_defects_uri"] == (
        "s3://b/lakehouse/landing/d/expected_defects.json")


class _Glue:
    class exceptions:
        ConcurrentRunsExceededException = type("ConcurrentRunsExceededException", (Exception,), {})
        EntityNotFoundException = type("EntityNotFoundException", (Exception,), {})

    def __init__(self, refusals, error=None):
        self.refusals, self.error, self.starts = refusals, error, 0

    def start_job_run(self, JobName, Arguments):
        self.starts += 1
        if self.error:
            raise self.error
        if self.starts <= self.refusals:
            raise self.exceptions.ConcurrentRunsExceededException()
        return {"JobRunId": "jr_1"}

    def get_job_run(self, JobName, RunId):
        return {"JobRun": {"JobRunState": "SUCCEEDED"}}


def test_a_back_to_back_run_waits_out_the_previous_runs_concurrency_slot():
    g, slept = _Glue(refusals=2), []
    assert lh._run_job(g, "j", {}, sleep=slept.append) == ("SUCCEEDED", "")
    assert g.starts == 3 and slept == [15, 30]


def test_only_the_concurrency_refusal_is_retried():
    g = _Glue(refusals=0, error=_Glue.exceptions.EntityNotFoundException())
    with pytest.raises(_Glue.exceptions.EntityNotFoundException):
        lh._run_job(g, "j", {}, sleep=lambda s: None)
    assert g.starts == 1
    g = _Glue(refusals=99)
    with pytest.raises(_Glue.exceptions.ConcurrentRunsExceededException):
        lh._run_job(g, "j", {}, sleep=lambda s: None)
    assert g.starts == 6
