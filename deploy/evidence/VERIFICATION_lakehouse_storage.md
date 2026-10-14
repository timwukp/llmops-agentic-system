# VERIFICATION — lakehouse storage (Phase 1 PR-04), 2026-10-14

**What was deployed.** `python deploy/10_lakehouse.py --region us-east-1`, from `main` plus this
change. Design: docs/research/LAKEHOUSE_LLM_LIFECYCLE.md §4.3; substrate proved by spike S7.

| Resource | Name |
|---|---|
| KMS keys (rotation on) | `alias/llmops-lakehouse-raw`, `alias/llmops-lakehouse-curated` |
| Table buckets (SSE-KMS) | `llmops-raw` (namespace `bronze`), `llmops-curated` (`silver`, `gold`) |
| Tables (Iceberg v2) | `bronze.{conversation_events, crm_cdc, erp_cdc, inference_logs, feedback}`, `silver.instruction_pairs`, `gold.{qlora_train, qlora_eval, dataset_manifest}` |
| Athena workgroup | `llmops-lakehouse` (engine v3, results `s3://<DATA_BUCKET>/athena/lakehouse/`, enforced) |
| IAM role | `llmops-lakehouse-glue` (for the PR-07 Glue jobs) |
| SSM | `/llmops/lakehouse/{raw_bucket_arn, curated_bucket_arn, raw_kms_key_arn, curated_kms_key_arn, workgroup, glue_role_arn}` |

**Idempotency.** The first run created everything up to the IAM role and then failed: `CreateRole`
rejected the description, because its pattern excludes `→`. That was fixed and the script run again:
every key, bucket, namespace and table reported `exists`, the role was created, and the SSM
parameters were written. A third run (not shown) changed nothing.

## `--verify`: 34 / 34 PASS

Each check reads the live resource back. Nothing here is taken from the script's own output.

| Group | Checks | What is read |
|---|---|---|
| Encryption | 2 | `GetTableBucketEncryption`: `aws:kms` with a key ARN, both buckets |
| Tables | 9 | the current Iceberg metadata file of each table: `format-version` 2, the column list exactly as declared, the default partition spec exactly as declared (`ingest_day` / `event_day` / `lang`, none for gold) |
| Athena | 9 | `SELECT count(*)` on every table through workgroup `llmops-lakehouse` and the `s3tablescatalog/<bucket>` catalog: SUCCEEDED (empty tables) |
| Glue role, allowed | 6 | `simulate_principal_policy` as `llmops-lakehouse-glue`: `s3tables:PutTableData` on a bronze table, `UpdateTableMetadataLocation` on curated, `glue:UpdateTable` on `silver.instruction_pairs`, `kms:Decrypt` (raw key), `kms:GenerateDataKey` (curated key), `s3:PutObject` under `lakehouse/glue/` |
| Glue role, denied | 7 | `s3tables:DeleteTable`, `DeleteTableBucket`, `PutTableData` on a bucket that is not ours, `glue:DeleteTable`, `kms:ScheduleKeyDeletion`, `s3:GetObject` on `customer-data/`, `iam:PassRole` |
| Retention | 1 | `GetTableMaintenanceConfiguration` on `bronze.conversation_events`: `maxSnapshotAgeHours` 72 |

The denials are the half that shows the role is scoped. A role with `s3tables:*` would pass every
"allowed" check.

## Decisions recorded

- **One key per bucket, not one key.** Being able to decrypt the raw key is the same as having
  access to the PII in Bronze. With a single key, any principal that may read the de-identified
  Gold tables could read Bronze too.
- **Key policies name only the account and the S3 Tables maintenance principal.** The maintenance
  grant is scoped by `kms:EncryptionContext:aws:s3:arn` to the key's own bucket; without that grant,
  CreateTable on an SSE-KMS bucket fails. Callers get key use through their IAM policies, which is
  where least privilege can be read and tested (the Glue role above).
- **Raw retention is short; curated retention keeps the defaults.** Bronze snapshots expire after
  72 h, and the raw bucket removes unreferenced and noncurrent files after 1 day, so a
  right-to-be-forgotten `DELETE` becomes a physical delete within days. Training reproducibility
  does not need long-lived snapshots: it rests on the `UNLOAD` extract and its sha256 (§4.4,
  PR-08).
- **No Lake Formation.** IAM-only grants suffice, which S7 showed live; the integration stays in
  IAM mode.

## Not covered here

- **Writing data:** the Glue jobs that write the tables arrive in PR-07, with the generator (PR-06).
  The role's grants are proved by simulation here; PR-07's evidence must include a real write.
- **The harnesses:** their read-only Gold access is PR-09.
