# Spec: S7 evidence and fixes

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. `deploy/evidence/SPIKE_07_s7_lakehouse_substrate.md`: run 3 result table, observations, run history
   (runs 1–3), consequences for Phase 1, live teardown re-check.
2. S7 uses a per-run table bucket name (`BUCKET_PREFIX` + `YYYYMMDD-HHMMSS`): a deleted table bucket's
   name is briefly not reusable.
3. Evidence rendering redacts registered values and groups digit runs of 13+.
4. PROJECT_STATE records the account-level `s3tablescatalog` integration.
