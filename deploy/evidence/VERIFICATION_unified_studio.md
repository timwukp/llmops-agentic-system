# VERIFICATION — SageMaker Unified Studio over the lakehouse, IAM-governed (2026-10-24)

## What the owner asked for

"好，開始 Unified Studio 檢查", then a new llmops domain, then (after the finding below) "IAM role
only": a sign-in to Unified Studio that shows the lakehouse, with Silver/Gold readable and Bronze
describe-only, and the pipeline untouched.

## Finding that changed the plan

The approved plan was to register the S3 Tables catalog with Lake Formation while keeping IAM access.
AWS documents no such mode:
- `lake-formation/latest/dg/change-access-iam-to-lf.html`: enabling Lake Formation access control
  "will revoke all existing IAM-based access to your S3 Tables resources", at step 1 (register), and
  "ensure hybrid access is false".
- Every pipeline principal (the lakehouse Glue role, the export's Athena queries, the console, the
  harnesses) reads the tables through IAM, so the register step alone would have stopped the pipeline.

An IAM-based domain reaches data through one execution IAM role per project
(`sagemaker-unified-studio/latest/adminguide/projects-iam-based-domains.html`), so the governance is
that role's IAM policy, and Lake Formation is not involved.

## The managed policy the console requires

For an existing execution role the console requires `SageMakerStudioAdminIAMDefaultExecutionPolicy`.
Read live (default version v27), and evaluated with `simulate_custom_policy` before any role existed:

| Case | managed policy alone | managed + `llmops-studio` inline |
|---|---|---|
| `s3tables:GetTableData` on silver.instruction_pairs | implicitDeny | **allowed** |
| `s3tables:GetTable` / `GetTableMetadataLocation` on bronze.conversation_events, `ListTables` on llmops-raw | implicitDeny | **allowed** |
| `s3tables:GetTableData` on bronze.conversation_events | implicitDeny | **explicitDeny** |
| `s3tables:PutTableData` / `DeleteTable` on silver | implicitDeny | **explicitDeny** |
| `glue:DeleteTable` on silver.instruction_pairs | **allowed** | explicitDeny |
| `glue:DeleteCatalog` on `s3tablescatalog` | **allowed** | explicitDeny |
| `lakeformation:RegisterResource`, `PutDataLakeSettings` | **allowed** | explicitDeny |
| `iam:PassRole` of `llmops-lakehouse-glue` (to glue), `llmops-sagemaker-execution` (to sagemaker) | **allowed** | explicitDeny |
| `iam:PassRole` of itself (to glue) | allowed | allowed |
| `iam:CreateRole` under `service-role/AmazonSageMaker*` | **allowed** | explicitDeny |

The managed policy alone would let a Studio user delete the catalog every lakehouse table hangs from,
register S3 Tables with Lake Formation (the outage above), or start a Glue job as the lakehouse role
and read Bronze through it. Hence the inline policy is mostly explicit Deny. 15/15 cases right with
it. The user guide names a different required policy, `SageMakerStudioAdminIAMPermissiveExecutionPolicy`
(v24); alone it gets 10 of the 15 wrong, including **reading Bronze rows**, and with the inline policy
0.

## Deployed (2026-10-24)

`deploy/11_studio.py --region us-east-1`: role `llmops-studio-execution` created, managed policy
attached, inline policy put, SSM `/llmops/studio/execution_role_arn`. Before the domain existed,
`--verify` passed 20/21; the 21st was the domain.

## Live: the domain, created by the owner in the console

The owner signed in as the federated role `testOnly` (AdministratorAccess: it covers
`SageMakerStudioAdminIAMConsolePolicy`, and simulated PassRole of this role to DataZone is
`allowed`), chose **Use another role → llmops-studio-execution**, the AWS-owned key, and **Set up**.
The wizard listed the role's four requirements; three were already met, and the fourth
("Recommended: Lake Formation administrator") was deliberately not granted. The Lake Formation admin
list afterwards is the same four principals as before; neither this role nor the login role was added.

How an IAM-based domain differs from the classic `demo` one, read with `GetDomain`:

| | classic `demo` | IAM-based (new) |
|---|---|---|
| id | `dzd_…` | `dzd-…` |
| `domainExecutionRole` | AmazonSageMakerDomainExecution | **null** |
| `serviceRole` | AmazonSageMakerDomainService | **llmops-studio-execution** |

`--verify`'s domain lookup compared `domainExecutionRole` only and missed it; it now accepts either
field (`tests/test_studio.py::test_an_iam_based_domain_is_found_by_its_service_role`).

**What the console called** (CloudTrail, the setup minute):

| Call | In the public SDK/CLI (boto3 1.43.57, CLI 2.36.30, API Reference) |
|---|---|
| `datazone:CreateDomain` with `"preferences": {"DOMAIN_MODE": "EXPRESS"}` and `"iamSignIns": ["IAM_ROLE","IAM_USER"]` | the call yes, **those two fields no** |
| `datazone:ProvisionDomain` (`provisioningRoleArn` = this role) | **no** |
| `datazone:StartAccountBootstrapAction` (the admin project, `autoPublish`, `enableS3TablesAnalyticsIntegration`) | **no** |
| `sagemaker:CreateDomain` (`authMode: IAM`), made by DataZone on the owner's behalf | yes |

So an API exists, and it is the console's own: undocumented, and not something a deploy script here
should depend on. `11_studio.py` keeps the domain a console step.

## Live: `--verify` after the domain, and after the catalog-listing fix

**23/23 PASS**: the managed policy; the inline policy byte-equal; 18 role cases asked of IAM as the
role; `s3tablescatalog` still defaulting new tables and databases to `IAM_ALLOWED_PRINCIPALS`; no
`arn:aws:s3tables:` location registered with Lake Formation; and the domain (AVAILABLE, id published
to SSM `/llmops/studio/domain_id`). The bootstrap action carried
`enableS3TablesAnalyticsIntegration: true`, and the catalog checks ran after it.

## Live: from inside Studio, as the project

| Query (Studio query editor, admin project) | Result |
|---|---|
| `SELECT count(*) FROM "s3tablescatalog/llmops-curated"."silver"."instruction_pairs"` | **rows returned** |
| `SELECT * FROM "s3tablescatalog/llmops-raw"."bronze"."conversation_events" LIMIT 5` | **refused**: `TABLE_NOT_FOUND` |

CloudTrail for the Bronze query: `s3tables:GetTableBucket`, `GetNamespace` and `GetTable` on
`llmops-raw/bronze/conversation_events` were all **allowed** for the role. So the refusal is not the
metadata grant. Inferred, not measured (row and metadata-file reads are CloudTrail *data* events, which
`lookup-events` does not show): the Iceberg metadata file that carries the column list is read with
`s3tables:GetTableData`, the same action as the rows, which `NoBronzeRows` denies, and Athena reports
an unreadable schema as a missing table.

**What that means for "describe-only Bronze":** the PII barrier holds (no Bronze row was returned), and
the column list is very likely NOT visible through the query engine under IAM, because in S3 Tables the
schema and the rows sit behind one action. Showing Bronze's columns without its rows is therefore a
catalog job: publish them as business metadata in SageMaker Catalog. Not done in this change.

**Catalog browsing:** the same CloudTrail showed `glue:GetCatalogs (recursive)` failing on every
listing, with the one AccessDenied being `s3tables:GetTableBucket` on the AWS-managed table bucket
`aws-s3` (S3 Metadata tables); Discover showed the registered models and the five lakehouse Glue jobs
but no lakehouse table. The inline policy now allows the metadata reads on `aws-s3`
(`BrowseAwsManagedBucket`) and denies rows everywhere outside Silver and Gold (`NoRowsOutsideCurated`;
the Permissive Studio policy grants rows on every bucket). Whether Discover then lists the lakehouse
tables is not yet observed.

## Not yet verified live

- Discover listing the lakehouse tables after the `aws-s3` grant.
- `SHOW COLUMNS` on a Bronze table, which would confirm or refute the schema inference above.
- A lakehouse run through the pipeline after the domain exists. (The one-click demo run
  `run-20261023T160741Z-65fec5d8` SUCCEEDED, but it finished before the domain was created, so it
  does not show the domain left the pipeline alone; the catalog checks above do, directly.)
