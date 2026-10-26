# Spec: the llmops Unified Studio execution role

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. **One role, `llmops-studio-execution`** (`deploy/11_studio.py`), idempotent, `--dry-run`,
   `--verify`, `--teardown`, SSM `/llmops/studio/execution_role_arn`, and `/llmops/studio/domain_id`
   once the domain exists.
   - Trust: the documented service list (12 services), scoped by `aws:SourceAccount`.
   - Managed policy: `SageMakerStudioAdminIAMDefaultExecutionPolicy`, which the console requires for
     an existing execution role.
2. **Inline policy `llmops-studio`.**
   - Allow: read `llmops-curated` (Silver, Gold) including `s3tables:GetTableData`; on `llmops-raw`
     (Bronze) only the metadata reads (bucket, namespace, list, table, metadata location); decrypt
     with the curated key only; Athena queries; pass itself to the documented services; metadata reads
     on the AWS-managed `aws-s3` table bucket, which Studio's recursive catalog listing reads.
   - Explicit Deny, because the managed policy allows most of these (read live, v27):
     Bronze `GetTableData`/`PutTableData`; every s3tables write on both buckets; every Glue write on
     `s3tablescatalog`; Lake Formation `RegisterResource`, `RegisterResourceWithPrivilegedAccess`,
     `DeregisterResource`, `UpdateResource`, `PutDataLakeSettings`; `iam:PassRole` of any role but
     itself; role creation and policy changes; `GetTableData` anywhere outside `llmops-curated`.
3. **The domain is not created by the script.** The only documented way to create an IAM-based
   domain is the SageMaker console's "Get started"; `CreateDomain` returns nothing that marks a
   domain IAM-based. The owner creates it, choosing this role. `--verify` finds it by the role, in
   `domainExecutionRole` or `serviceRole` (an IAM-based domain uses the latter).
4. **`--verify`** asks IAM as the role, both directions, and checks that `s3tablescatalog` still
   defaults new databases and tables to `IAM_ALLOWED_PRINCIPALS` and that no `arn:aws:s3tables:`
   location is registered with Lake Formation.
5. **`--teardown`** refuses while a domain still uses the role, removes the role from the Lake
   Formation admins if present, then deletes the role and the SSM parameters.
6. **Tests:** every `verify` case is decided by OUR policy (an allow case by an Allow, a deny case by
   an explicit Deny); negative controls for the PII Deny, Lake Formation, PassRole, the catalog Deny,
   describe-only Bronze, and the verify cases.
7. **Live:** create the role, `--verify`, the owner's console setup, a Studio query in each direction,
   and one lakehouse run through the pipeline; recorded in
   `deploy/evidence/VERIFICATION_unified_studio.md`.
