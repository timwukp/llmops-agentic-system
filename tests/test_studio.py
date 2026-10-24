"""deploy/11_studio.py, offline: the Unified Studio execution role reads Silver and Gold, sees
Bronze's tables but not its rows, and cannot undo the pipeline's access, whatever the AWS managed
policy it must carry grants.

The live proof is deploy/evidence/VERIFICATION_unified_studio.md: simulate_custom_policy with the
managed policy alone (it ALLOWS glue:DeleteCatalog on s3tablescatalog, Lake Formation
RegisterResource and PassRole of the pipeline's roles) and with this inline policy added (all 15
cases right). These tests pin the property that makes that hold: every case the role must NOT do is
refused by an explicit Deny in OUR policy, never by the managed policy happening not to grant it.
"""
from __future__ import annotations

import fnmatch
import importlib.util
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("studio", REPO / "deploy" / "11_studio.py")
st = importlib.util.module_from_spec(spec)
spec.loader.exec_module(st)

ACCOUNT, REGION = "123456789012", "us-east-1"  # documentation account
KEY = f"arn:aws:kms:{REGION}:{ACCOUNT}:key/curated"
SILVER = f"{st.bucket_arn(REGION, ACCOUNT, st.CURATED)}/table/1111"
BRONZE = f"{st.bucket_arn(REGION, ACCOUNT, st.RAW)}/table/2222"
POLICY = st.inline_policy(REGION, ACCOUNT, KEY)


def _list(v):
    return [v] if isinstance(v, str) else list(v)


def _matches(stmt, action, resource, ctx):
    if not any(fnmatch.fnmatchcase(action.lower(), a.lower()) for a in _list(stmt["Action"])):
        return False
    if "NotResource" in stmt:
        if any(fnmatch.fnmatchcase(resource, r) for r in _list(stmt["NotResource"])):
            return False
    elif not any(fnmatch.fnmatchcase(resource, r) for r in _list(stmt["Resource"])):
        return False
    cond = stmt.get("Condition", {}).get("StringEquals", {})
    return all((ctx or {}).get(k) in _list(v) for k, v in cond.items())


def decide(action, resource, ctx=None, policy=POLICY):
    """The inline policy on its own: explicitDeny / allowed / implicitDeny."""
    hits = [s for s in policy["Statement"] if _matches(s, action, resource, ctx)]
    if any(s["Effect"] == "Deny" for s in hits):
        return "explicitDeny"
    return "allowed" if hits else "implicitDeny"


def _ctx(entries):
    return {e["ContextKeyName"]: e["ContextKeyValues"][0] for e in entries or []}


# ── the cases verify() asks IAM are the contract ─────────────────────────────
@pytest.mark.parametrize("case", st.role_cases(REGION, ACCOUNT, SILVER, BRONZE),
                         ids=lambda c: f"{c[0]}-{c[1].rsplit('/', 1)[-1]}-{'allow' if c[2] else 'deny'}")
def test_every_case_is_decided_by_this_policy(case):
    action, resource, allow, ctx = case
    got = decide(action, resource, _ctx(ctx))
    if allow:
        assert got == "allowed", f"{action} must be granted here: the managed policy does not grant it"
    else:
        assert got == "explicitDeny", (
            f"{action} on {resource} must be an explicit Deny: the managed policy the console requires "
            "ALLOWS most of these, so an omission here is a grant")


def test_the_cases_cover_both_directions_for_bronze():
    cases = st.role_cases(REGION, ACCOUNT, SILVER, BRONZE)
    bronze = {(a, allow) for a, r, allow, _ in cases if r == BRONZE}
    assert ("s3tables:GetTableData", False) in bronze, "the PII barrier is not checked"
    assert ("s3tables:GetTable", True) in bronze, "describe-only Bronze is not checked"


# ── the PII barrier ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("action", ["s3tables:GetTableData", "s3tables:PutTableData"])
def test_no_bronze_row_can_be_read_or_written(action):
    assert decide(action, BRONZE) == "explicitDeny"
    assert decide(action, st.bucket_arn(REGION, ACCOUNT, st.RAW)) == "explicitDeny"


@pytest.mark.parametrize("action", st.READ)
def test_bronze_tables_and_columns_stay_visible(action):
    for resource in (BRONZE, st.bucket_arn(REGION, ACCOUNT, st.RAW)):
        assert decide(action, resource) == "allowed", (
            f"{action} on Bronze: the owner asked for describe-only, not invisible")


def test_silver_and_gold_rows_are_readable_and_nothing_else_in_the_account_is():
    assert decide("s3tables:GetTableData", SILVER) == "allowed"
    other = f"arn:aws:s3tables:{REGION}:{ACCOUNT}:bucket/someone-else/table/x"
    assert decide("s3tables:GetTableData", other) == "explicitDeny", (
        "the Permissive Studio policy grants rows on every bucket; only an explicit Deny holds")


def test_the_catalog_browser_can_list_the_aws_managed_bucket_but_not_its_rows():
    aws_s3 = st.bucket_arn(REGION, ACCOUNT, st.AWS_MANAGED_BUCKET)
    assert decide("s3tables:GetTableBucket", aws_s3) == "allowed"
    assert decide("s3tables:GetTableData", f"{aws_s3}/table/x") == "explicitDeny"


def test_the_raw_key_is_not_granted_and_no_key_is_granted_broadly():
    keys = [s for s in POLICY["Statement"] if s["Effect"] == "Allow"
            and any(a.startswith("kms:") for a in _list(s["Action"]))]
    assert [_list(s["Resource"]) for s in keys] == [[KEY]]


# ── nothing the pipeline relies on can be changed from Studio ────────────────
@pytest.mark.parametrize("action", ["s3tables:PutTableData", "s3tables:UpdateTableMetadataLocation",
                                    "s3tables:DeleteTable", "s3tables:DeleteTableBucket",
                                    "s3tables:PutTableBucketPolicy", "s3tables:CreateTable",
                                    "s3tables:RenameTable", "s3tables:DeleteNamespace"])
def test_the_lakehouse_is_read_only_from_studio(action):
    for resource in (SILVER, BRONZE, st.bucket_arn(REGION, ACCOUNT, st.CURATED)):
        assert decide(action, resource) == "explicitDeny", f"{action} on {resource}"


@pytest.mark.parametrize("action", ["glue:DeleteCatalog", "glue:UpdateCatalog", "glue:DeleteTable",
                                    "glue:UpdateTable", "glue:CreateTable", "glue:BatchDeleteTable",
                                    "glue:DeleteDatabase"])
def test_the_federated_catalog_cannot_be_changed(action):
    g = f"arn:aws:glue:{REGION}:{ACCOUNT}"
    for resource in (f"{g}:catalog/s3tablescatalog", f"{g}:catalog/s3tablescatalog/llmops-curated",
                     f"{g}:database/s3tablescatalog/llmops-curated/silver",
                     f"{g}:table/s3tablescatalog/llmops-raw/bronze/feedback"):
        assert decide(action, resource) == "explicitDeny", f"{action} on {resource}"


@pytest.mark.parametrize("action", st.LF_REWIRING)
def test_lake_formation_cannot_be_rewired(action):
    """Registering S3 Tables revokes every IAM principal's access at once (AWS's migration page),
    so this role must never be able to do it, nor to make an admin who could."""
    assert decide(action, "*") == "explicitDeny"


@pytest.mark.parametrize("role", ["llmops-lakehouse-glue", "llmops-lakehouse-generator",
                                  "llmops-sagemaker-execution", "llmops-harness-execution",
                                  "service-role/AmazonSageMakerAnything"])
def test_no_other_role_can_be_passed(role):
    for svc in ("glue", "sagemaker", "bedrock", "athena"):
        assert decide("iam:PassRole", f"arn:aws:iam::{ACCOUNT}:role/{role}",
                      {"iam:PassedToService": f"{svc}.amazonaws.com"}) == "explicitDeny"


def test_the_role_may_pass_only_itself_and_only_to_the_documented_services():
    me = st.role_arn(ACCOUNT)
    assert decide("iam:PassRole", me, {"iam:PassedToService": "glue.amazonaws.com"}) == "allowed"
    assert decide("iam:PassRole", me, {"iam:PassedToService": "lambda.amazonaws.com"}) == "implicitDeny"


@pytest.mark.parametrize("action", st.IAM_ESCALATION)
def test_no_new_or_wider_role(action):
    assert decide(action, f"arn:aws:iam::{ACCOUNT}:role/service-role/AmazonSageMakerX") == "explicitDeny"


def test_every_deny_is_a_deny_on_purpose():
    """A Deny that names no resource we own would be dead weight; one on '*' must be one of the
    control-plane lists, not a broad action that would also break Studio's own work."""
    for s in POLICY["Statement"]:
        if s["Effect"] == "Deny" and _list(s.get("Resource", [])) == ["*"]:
            assert set(_list(s["Action"])) <= set(st.LF_REWIRING) | set(st.IAM_ESCALATION), s["Sid"]


# ── trust, managed policy, plan ──────────────────────────────────────────────
def test_trust_is_the_documented_one_and_scoped_to_this_account():
    t = st.trust_policy(ACCOUNT)["Statement"][0]
    assert t["Condition"] == {"StringEquals": {"aws:SourceAccount": ACCOUNT}}
    assert len(t["Principal"]["Service"]) == 12 and "datazone.amazonaws.com" in t["Principal"]["Service"]
    assert "sts:SetSourceIdentity" in t["Action"]


def test_the_managed_policy_is_the_one_the_console_requires():
    assert st.MANAGED == "arn:aws:iam::aws:policy/SageMakerStudioAdminIAMDefaultExecutionPolicy"


def test_the_dry_run_needs_no_aws(capsys):
    assert st.main(["--region", REGION, "--account-id", ACCOUNT, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "NOT created here: the domain" in out and '"NoBronzeRows"' in out


# ── teardown ─────────────────────────────────────────────────────────────────
class _DZ:
    def __init__(self, role, field="domainExecutionRole"):
        self.role, self.field = role, field

    def get_paginator(self, name):
        class P:
            def paginate(self_inner):
                return [{"items": [{"id": "dzd_x"}]}]
        return P()

    def get_domain(self, identifier):
        return {"id": identifier, self.field: self.role, "status": "AVAILABLE"}


class _Session:
    def __init__(self, role):
        self.dz = _DZ(role)

    def client(self, name, **_):
        assert name == "datazone", f"teardown touched {name} before refusing"
        return self.dz


def test_teardown_refuses_while_a_domain_runs_on_the_role():
    with pytest.raises(SystemExit, match="delete the domain first"):
        st.teardown(_Session(st.role_arn(ACCOUNT)), REGION, ACCOUNT)


def test_find_domain_matches_on_the_execution_role_only():
    assert st.find_domain(_DZ(st.role_arn(ACCOUNT)), ACCOUNT)["id"] == "dzd_x"
    assert st.find_domain(_DZ(f"arn:aws:iam::{ACCOUNT}:role/someone-else"), ACCOUNT) is None


def test_an_iam_based_domain_is_found_by_its_service_role():
    """Live shape (2026-10-24): `domainExecutionRole` null, the execution role in `serviceRole`."""
    assert st.find_domain(_DZ(st.role_arn(ACCOUNT), "serviceRole"), ACCOUNT)["id"] == "dzd_x"
    with pytest.raises(SystemExit, match="delete the domain first"):
        st.teardown(type("S", (), {"client": lambda self, n, **_: _DZ(st.role_arn(ACCOUNT), "serviceRole")})(),
                    REGION, ACCOUNT)
