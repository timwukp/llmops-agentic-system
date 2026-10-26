#!/usr/bin/env python3
"""11_studio.py — the execution role for the llmops SageMaker Unified Studio domain (idempotent).

What it is for: a person signs in to Unified Studio and browses and queries the lakehouse. They
can read Silver and Gold (de-identified), and in Bronze (raw PII) they can see that the tables
exist and what their columns are, but not read a row. The pipeline's own access does not change.

Why IAM and not Lake Formation. The approved plan was to register the S3 Tables catalog with Lake
Formation "keeping IAM access". AWS's own migration page says the opposite happens: "Enabling AWS
Lake Formation access control will revoke all existing IAM-based access to your S3 Tables
resources", immediately, at the register step
(lake-formation/latest/dg/change-access-iam-to-lf.html), and hybrid access mode is not offered
for S3 Tables (the same page: "ensure hybrid access is false"). Every pipeline principal reads the
tables through IAM today. An IAM-based Unified Studio domain, on the other hand, reaches data
through one execution IAM role per project (sagemaker-unified-studio/latest/adminguide/
projects-iam-based-domains.html), so the governance here is that role's IAM policy, and Lake
Formation stays out of it. Coarser (no column or row filters), and nothing that works today moves.

Why most of the inline policy is Deny. The console's "use an existing role" path requires the AWS
managed policy SageMakerStudioAdminIAMDefaultExecutionPolicy on the role. Read live (v27), it
grants, among much else:
  - lakeformation:RegisterResource / DeregisterResource / PutDataLakeSettings on "*": the role
    itself could register S3 Tables with Lake Formation, which is the outage above;
  - iam:PassRole on "*" in the account to glue/sagemaker/athena/...: it could start a Glue job as
    llmops-lakehouse-glue and read Bronze through that role;
  - iam:CreateRole + AttachRolePolicy under service-role/AmazonSageMaker* with the Studio
    *Permissive* policies: a second route to a broader role.
So the scoping cannot rest on what we Allow; it rests on explicit Denies, which no Allow in any
attached policy overrides. `verify` asks IAM as the role (simulate_principal_policy, managed policy
included), not the JSON.

The domain itself is NOT created here. CreateDomain returns nothing that marks a domain IAM-based
(the account's classic `demo` domain reads exactly as a new one would: V2, SSO DISABLED), and the
only documented way to create an IAM-based domain is the SageMaker console's "Get started" (one per
account per Region). The owner does that once, choosing this role as the execution role; `verify`
then finds the domain by its execution role and publishes it.

Creates:
  - IAM role llmops-studio-execution: the documented trust policy (SourceAccount-scoped), the
    required managed policy, inline policy `llmops-studio`
  - SSM /llmops/studio/execution_role_arn (deploy) and /llmops/studio/domain_id (verify, once the
    domain exists)

No standing cost: an IAM role costs nothing, and Unified Studio has no per-user charge.

Usage:
  python deploy/11_studio.py --region us-east-1 --dry-run [--account-id <id>]
  python deploy/11_studio.py --region us-east-1
  python deploy/11_studio.py --region us-east-1 --verify
  python deploy/11_studio.py --region us-east-1 --teardown [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys

PROJECT_TAG = {"project": "llmops-agentic-system"}
ROLE = "llmops-studio-execution"
INLINE = "llmops-studio"
#: Required by the console for an existing execution role (setup-iam-based-domains.html).
MANAGED = "arn:aws:iam::aws:policy/SageMakerStudioAdminIAMDefaultExecutionPolicy"
RAW, CURATED = "llmops-raw", "llmops-curated"
CURATED_KEY_ALIAS = "alias/llmops-lakehouse-curated"
CATALOG = "s3tablescatalog"
#: The AWS-managed table bucket for S3 Metadata tables; Studio's catalog listing reads it.
AWS_MANAGED_BUCKET = "aws-s3"
SSM_PREFIX = "/llmops/studio"
#: The services the documented trust policy names, verbatim.
TRUSTED_SERVICES = (
    "datazone", "sagemaker", "glue", "bedrock", "scheduler", "lakeformation", "airflow-serverless",
    "athena", "redshift", "emr-serverless", "access-grants.s3", "elasticmapreduce")
#: PassRoleSelf, as documented: the role may pass only itself, only to these.
PASS_TO = ("sagemaker", "glue", "lakeformation", "bedrock", "scheduler", "airflow-serverless",
           "athena", "redshift", "emr-serverless", "access-grants.s3")
READ = ["s3tables:GetTableBucket", "s3tables:GetNamespace", "s3tables:ListNamespaces",
        "s3tables:ListTables", "s3tables:GetTable", "s3tables:GetTableMetadataLocation"]
#: Each a calls the Lake Formation control plane could use to move the S3 Tables catalog out of IAM
#: mode, or to make some principal an administrator who could.
LF_REWIRING = ["lakeformation:RegisterResource", "lakeformation:RegisterResourceWithPrivilegedAccess",
               "lakeformation:DeregisterResource", "lakeformation:UpdateResource",
               "lakeformation:PutDataLakeSettings"]
IAM_ESCALATION = ["iam:CreateRole", "iam:AttachRolePolicy", "iam:PutRolePolicy", "iam:DeleteRolePolicy",
                  "iam:DetachRolePolicy", "iam:UpdateAssumeRolePolicy", "iam:PutRolePermissionsBoundary",
                  "iam:DeleteRolePermissionsBoundary", "iam:CreatePolicyVersion"]


def bucket_arn(region: str, account_id: str, name: str) -> str:
    return f"arn:aws:s3tables:{region}:{account_id}:bucket/{name}"


def role_arn(account_id: str) -> str:
    return f"arn:aws:iam::{account_id}:role/{ROLE}"


def trust_policy(account_id: str) -> dict:
    return {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": [f"{s}.amazonaws.com" for s in TRUSTED_SERVICES]},
        "Action": ["sts:AssumeRole", "sts:TagSession", "sts:SetContext", "sts:SetSourceIdentity"],
        "Condition": {"StringEquals": {"aws:SourceAccount": account_id}}}]}


def inline_policy(region: str, account_id: str, curated_key_arn: str) -> dict:
    raw, cur = bucket_arn(region, account_id, RAW), bucket_arn(region, account_id, CURATED)
    aws_s3 = bucket_arn(region, account_id, AWS_MANAGED_BUCKET)
    glue = f"arn:aws:glue:{region}:{account_id}"
    catalog = [f"{glue}:catalog/{CATALOG}", f"{glue}:catalog/{CATALOG}/*",
               f"{glue}:database/{CATALOG}/*", f"{glue}:table/{CATALOG}/*"]
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "BrowseTableBuckets", "Effect": "Allow", "Action": "s3tables:ListTableBuckets",
         "Resource": "*"},
        {"Sid": "ReadSilverAndGold", "Effect": "Allow", "Action": READ + ["s3tables:GetTableData"],
         "Resource": [cur, f"{cur}/table/*"]},
        {"Sid": "DescribeBronze", "Effect": "Allow", "Action": READ, "Resource": [raw, f"{raw}/table/*"]},
        # Studio's catalog browser lists every table bucket recursively (glue:GetCatalogs), and one
        # failing bucket fails the whole listing: live, the AWS-managed `aws-s3` bucket (S3
        # Metadata tables) was the only AccessDenied, and Discover showed no lakehouse table.
        # Metadata only; its rows fall under NoRowsOutsideCurated.
        {"Sid": "BrowseAwsManagedBucket", "Effect": "Allow", "Action": READ,
         "Resource": [aws_s3, f"{aws_s3}/table/*"]},
        {"Sid": "CuratedKey", "Effect": "Allow", "Action": "kms:Decrypt", "Resource": curated_key_arn},
        {"Sid": "QueryThroughAthena", "Effect": "Allow",
         "Action": ["athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults",
                    "athena:StopQueryExecution", "athena:GetWorkGroup"],
         "Resource": f"arn:aws:athena:{region}:{account_id}:workgroup/*"},
        {"Sid": "PassRoleSelf", "Effect": "Allow", "Action": "iam:PassRole",
         "Resource": role_arn(account_id),
         "Condition": {"StringEquals": {"iam:PassedToService": [f"{s}.amazonaws.com" for s in PASS_TO]}}},
        # ── explicit Denies: these hold whatever the managed policy grants ──
        # The PII barrier. A row of an S3 Table is only ever read through GetTableData, so this is
        # the one action that separates "sees the table" from "reads the table". The raw KMS key is
        # not denied: the managed policy already lets S3 use any key for the account, and denying
        # the key could take the Bronze column list with it, which the owner asked to keep.
        {"Sid": "NoBronzeRows", "Effect": "Deny", "Action": ["s3tables:GetTableData", "s3tables:PutTableData"],
         "Resource": [raw, f"{raw}/table/*"]},
        # And no rows anywhere but Silver and Gold: the Permissive Studio policy grants
        # s3tables data access on every bucket.
        {"Sid": "NoRowsOutsideCurated", "Effect": "Deny", "Action": "s3tables:GetTableData",
         "NotResource": [cur, f"{cur}/table/*"]},
        # The pipeline owns the lakehouse; a Studio user reads it.
        {"Sid": "NoLakehouseWrites", "Effect": "Deny",
         "Action": ["s3tables:Put*", "s3tables:Delete*", "s3tables:Create*", "s3tables:Update*",
                    "s3tables:Rename*"],
         "Resource": [raw, f"{raw}/table/*", cur, f"{cur}/table/*"]},
        {"Sid": "NoCatalogWrites", "Effect": "Deny",
         "Action": ["glue:Create*", "glue:Update*", "glue:Delete*", "glue:BatchCreate*",
                    "glue:BatchUpdate*", "glue:BatchDelete*"],
         "Resource": catalog},
        {"Sid": "NoLakeFormationRewiring", "Effect": "Deny", "Action": LF_REWIRING, "Resource": "*"},
        {"Sid": "NoOtherRoles", "Effect": "Deny", "Action": "iam:PassRole",
         "NotResource": role_arn(account_id)},
        {"Sid": "NoNewOrWiderRoles", "Effect": "Deny", "Action": IAM_ESCALATION, "Resource": "*"},
    ]}


def plan(region: str, account_id: str) -> list[str]:
    return [f"iam role {role_arn(account_id)} (trust: {len(TRUSTED_SERVICES)} services, "
            f"aws:SourceAccount)",
            f"attach managed policy {MANAGED.rsplit('/', 1)[-1]} (required by the console)",
            f"inline policy {INLINE}: read {CURATED} (silver, gold); describe {RAW} (bronze); "
            "deny Bronze rows, lakehouse writes, catalog writes, Lake Formation rewiring, other roles",
            f"ssm {SSM_PREFIX}/execution_role_arn",
            "NOT created here: the domain (console 'Get started', choose this role)"]


# ── live ─────────────────────────────────────────────────────────────────────
def curated_key(kms) -> str:
    return kms.describe_key(KeyId=CURATED_KEY_ALIAS)["KeyMetadata"]["Arn"]


def deploy(session, region: str, account_id: str) -> dict:
    iam, out = session.client("iam"), {}
    try:
        arn = iam.get_role(RoleName=ROLE)["Role"]["Arn"]
        iam.update_assume_role_policy(RoleName=ROLE, PolicyDocument=json.dumps(trust_policy(account_id)))
        out["role"] = "exists (trust re-applied)"
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(trust_policy(account_id)),
                              Description="llmops SageMaker Unified Studio execution role (deploy/11_studio.py)",
                              Tags=[{"Key": k, "Value": v} for k, v in PROJECT_TAG.items()])["Role"]["Arn"]
        out["role"] = "created"
    attached = {p["PolicyArn"] for p in iam.list_attached_role_policies(RoleName=ROLE)["AttachedPolicies"]}
    if MANAGED not in attached:
        iam.attach_role_policy(RoleName=ROLE, PolicyArn=MANAGED)
    out["managed policy"] = "attached" if MANAGED not in attached else "exists"
    key = curated_key(session.client("kms", region_name=region))
    iam.put_role_policy(RoleName=ROLE, PolicyName=INLINE,
                        PolicyDocument=json.dumps(inline_policy(region, account_id, key)))
    out["inline policy"] = "put"
    session.client("ssm", region_name=region).put_parameter(
        Name=f"{SSM_PREFIX}/execution_role_arn", Value=arn, Type="String", Overwrite=True)
    out["ssm"] = f"{SSM_PREFIX}/execution_role_arn"
    return out


def role_cases(region: str, account_id: str, silver_table: str, bronze_table: str) -> list[tuple]:
    """(action, resource, should_be_allowed, context) asked of IAM as the role."""
    glue = f"arn:aws:glue:{region}:{account_id}"
    to = lambda svc: [{"ContextKeyName": "iam:PassedToService", "ContextKeyType": "string",  # noqa: E731
                       "ContextKeyValues": [f"{svc}.amazonaws.com"]}]
    return [
        ("s3tables:GetTableData", silver_table, True, None),
        ("s3tables:GetTable", bronze_table, True, None),
        ("s3tables:GetTableMetadataLocation", bronze_table, True, None),
        ("s3tables:ListTables", bucket_arn(region, account_id, RAW), True, None),
        ("s3tables:GetTableBucket", bucket_arn(region, account_id, AWS_MANAGED_BUCKET), True, None),
        ("s3tables:GetTableData", f"{bucket_arn(region, account_id, AWS_MANAGED_BUCKET)}/table/x", False, None),
        ("s3tables:GetTableData", f"arn:aws:s3tables:{region}:{account_id}:bucket/someone-else/table/x", False, None),
        ("s3tables:GetTableData", bronze_table, False, None),
        ("s3tables:PutTableData", silver_table, False, None),
        ("s3tables:DeleteTable", silver_table, False, None),
        ("glue:DeleteTable", f"{glue}:table/{CATALOG}/{CURATED}/silver/instruction_pairs", False, None),
        ("glue:DeleteCatalog", f"{glue}:catalog/{CATALOG}", False, None),
        ("lakeformation:RegisterResource", "*", False, None),
        ("lakeformation:PutDataLakeSettings", "*", False, None),
        ("iam:PassRole", f"arn:aws:iam::{account_id}:role/llmops-lakehouse-glue", False, to("glue")),
        ("iam:PassRole", f"arn:aws:iam::{account_id}:role/llmops-sagemaker-execution", False, to("sagemaker")),
        ("iam:PassRole", role_arn(account_id), True, to("glue")),
        ("iam:CreateRole", f"arn:aws:iam::{account_id}:role/service-role/AmazonSageMakerX", False, None),
    ]


def pipeline_access_unchanged(session, region: str) -> dict:
    """The thing the Lake Formation route would have broken, asserted directly: the S3 Tables
    catalog still hands IAM principals their access, and no S3 Tables location is registered."""
    cat = session.client("glue", region_name=region).get_catalog(CatalogId=CATALOG)["Catalog"]
    iam_default = lambda perms: any(p.get("Principal", {}).get("DataLakePrincipalIdentifier")  # noqa: E731
                                    == "IAM_ALLOWED_PRINCIPALS" for p in perms or [])
    lf = session.client("lakeformation", region_name=region)
    registered, token = [], None
    while True:
        page = lf.list_resources(**({"NextToken": token} if token else {}))
        registered += [r["ResourceArn"] for r in page.get("ResourceInfoList", [])]
        token = page.get("NextToken")
        if not token:
            break
    return {
        "s3tablescatalog: new tables default to IAM access": iam_default(cat.get("CreateTableDefaultPermissions")),
        "s3tablescatalog: new databases default to IAM access": iam_default(cat.get("CreateDatabaseDefaultPermissions")),
        "no S3 Tables location registered with Lake Formation": not any(
            a.startswith("arn:aws:s3tables:") for a in registered) or "registered",
    }


def find_domain(dz, account_id: str) -> dict | None:
    """An IAM-based domain carries its execution role as `serviceRole`, with `domainExecutionRole`
    null and an id `dzd-…` (a classic domain is `dzd_…` with both roles set): measured on the
    domain "Get started" created 2026-10-24. Either field naming this role counts."""
    want = role_arn(account_id)
    for page in dz.get_paginator("list_domains").paginate():
        for d in page["items"]:
            g = dz.get_domain(identifier=d["id"])
            if want in (g.get("domainExecutionRole"), g.get("serviceRole")):
                return g
    return None


def verify(session, region: str, account_id: str) -> dict:
    iam, checks = session.client("iam"), {}
    s3t = session.client("s3tables", region_name=region)
    arns = {b["name"]: b["arn"] for b in s3t.list_table_buckets()["tableBuckets"]}
    table = lambda b, ns, n: s3t.get_table(tableBucketARN=arns[b], namespace=ns, name=n)["tableARN"]  # noqa: E731
    silver, bronze = table(CURATED, "silver", "instruction_pairs"), table(RAW, "bronze", "conversation_events")
    attached = {p["PolicyArn"] for p in iam.list_attached_role_policies(RoleName=ROLE)["AttachedPolicies"]}
    checks["managed policy attached"] = MANAGED in attached or sorted(attached)
    live = iam.get_role_policy(RoleName=ROLE, PolicyName=INLINE)["PolicyDocument"]
    want = inline_policy(region, account_id, curated_key(session.client("kms", region_name=region)))
    checks["inline policy is this script's"] = live == want or "drifted"
    for action, resource, allow, ctx in role_cases(region, account_id, silver, bronze):
        kw = {"ContextEntries": ctx} if ctx else {}
        got = iam.simulate_principal_policy(PolicySourceArn=role_arn(account_id), ActionNames=[action],
                                            ResourceArns=[resource], **kw)["EvaluationResults"][0]["EvalDecision"]
        label = resource.rsplit("/", 1)[-1] if "/" in resource else resource.rsplit(":", 1)[-1]
        checks[f"role {'may' if allow else 'may NOT'} {action} on {label[:48]}"] = (
            (got == "allowed") == allow or got)
    checks.update(pipeline_access_unchanged(session, region))
    d = find_domain(session.client("datazone", region_name=region), account_id)
    if d:
        session.client("ssm", region_name=region).put_parameter(
            Name=f"{SSM_PREFIX}/domain_id", Value=d["id"], Type="String", Overwrite=True)
        checks["domain uses this role"] = d["status"] == "AVAILABLE" or d["status"]
    else:
        checks["domain uses this role"] = "PENDING: SageMaker console > Get started, choose this role"
    return checks


def teardown(session, region: str, account_id: str) -> dict:
    """Refuses while a domain still runs on the role: deleting a role that is a live domain's
    execution role (or a Lake Formation admin) is what broke project create and delete in S7."""
    if find_domain(session.client("datazone", region_name=region), account_id):
        raise SystemExit("a Unified Studio domain still uses this role: delete the domain first")
    iam, out = session.client("iam"), {}
    lf = session.client("lakeformation", region_name=region)
    settings = lf.get_data_lake_settings()["DataLakeSettings"]
    admins = settings.get("DataLakeAdmins", [])
    keep = [a for a in admins if a.get("DataLakePrincipalIdentifier") != role_arn(account_id)]
    if len(keep) != len(admins):
        lf.put_data_lake_settings(DataLakeSettings={**settings, "DataLakeAdmins": keep})
        out["lake formation admin"] = "removed"
    try:
        iam.delete_role_policy(RoleName=ROLE, PolicyName=INLINE)
    except iam.exceptions.NoSuchEntityException:
        pass
    try:
        iam.detach_role_policy(RoleName=ROLE, PolicyArn=MANAGED)
    except iam.exceptions.NoSuchEntityException:
        pass
    try:
        iam.delete_role(RoleName=ROLE)
        out["role"] = "deleted"
    except iam.exceptions.NoSuchEntityException:
        out["role"] = "absent"
    ssm = session.client("ssm", region_name=region)
    for k in ("execution_role_arn", "domain_id"):
        try:
            ssm.delete_parameter(Name=f"{SSM_PREFIX}/{k}")
        except ssm.exceptions.ParameterNotFound:
            pass
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--region", required=True)
    ap.add_argument("--account-id", help="skip STS (offline dry-run) and use this account id")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and the policies; no write")
    ap.add_argument("--verify", action="store_true", help="simulate the role in both directions, "
                    "check the pipeline's IAM access is intact, find the domain")
    ap.add_argument("--teardown", action="store_true", help="delete the role (refused while a domain uses it)")
    args = ap.parse_args(argv)
    session = None
    if not (args.dry_run and args.account_id):
        import boto3
        session = boto3.Session(region_name=args.region)
    account_id = args.account_id or session.client("sts").get_caller_identity()["Account"]
    if args.teardown:
        if args.dry_run:
            print(f"would delete: role {ROLE} (inline + managed detached), its Lake Formation admin "
                  f"entry if any, ssm {SSM_PREFIX}/*; refuses while a domain uses the role")
            return 0
        print(json.dumps(teardown(session, args.region, account_id), indent=2))
        return 0
    if args.dry_run:
        for item in plan(args.region, account_id):
            print(f"would ensure {item}")
        key = curated_key(session.client("kms", region_name=args.region)) if session else \
            f"arn:aws:kms:{args.region}:{account_id}:key/<{CURATED_KEY_ALIAS}>"
        print(json.dumps({"trust": trust_policy(account_id),
                          "inline": inline_policy(args.region, account_id, key)}, indent=2))
        return 0
    if args.verify:
        checks = verify(session, args.region, account_id)
        for k, v in checks.items():
            print(f"{'PASS' if v is True else 'FAIL'}  {k}" + ("" if isinstance(v, bool) else f": {v}"))
        return 0 if all(v is True for v in checks.values()) else 1
    print(json.dumps(deploy(session, args.region, account_id), indent=2))
    print("NEXT: SageMaker console > Unified Studio > Get started > existing execution role "
          f"{ROLE}; then python deploy/11_studio.py --region {args.region} --verify")
    return 0


if __name__ == "__main__":
    sys.exit(main())
