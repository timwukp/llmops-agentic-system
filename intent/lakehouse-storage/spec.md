# Spec: lakehouse storage

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. Table buckets `llmops-raw` (bronze) and `llmops-curated` (silver, gold), SSE-KMS, one rotating
   customer-managed key per bucket; key policies name only the account and the S3 Tables maintenance
   principal (encryption-context scoped to the bucket).
2. Ten Iceberg v2 tables (§2 and §4.3; §4.3's list omits `bronze.web_events`), lowercase, declared schemas and partition specs, no table tags.
3. Raw retention: snapshots 72 h, unreferenced/noncurrent files 1 day. Curated: defaults.
4. Athena workgroup `llmops-lakehouse`, enforced result location and encryption.
5. IAM role `llmops-lakehouse-glue`: S3 Tables data on the two buckets, the federated catalog
   entries, the two keys, its own script prefix and log groups. No Lake Formation.
6. SSM `/llmops/lakehouse/*`; `--dry-run` offline; `--verify`; `--teardown` (keys scheduled, not deleted).
7. Refuse, never enable, a missing `s3tablescatalog` integration.
