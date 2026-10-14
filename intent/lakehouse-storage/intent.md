# Intent: the lakehouse storage Phase 1 writes into

- **Slug:** lakehouse-storage
- **Author:** Claude (AI agent)
- **Date:** 2026-10-14
- **Accepted-by:** Tim WU
- **Status:** accepted

## Problem

Phase 1 (lakehouse first, owner decision 2026-10-13) needs standing S3 Tables storage before any
generator, Glue job or export can land data. Spike S7 proved the substrate; PR-04 of the Phase 1
plan builds it. The owner asked to continue in the agreed order after #156 (2026-10-14).

## Desired outcome

Bronze, Silver and Gold tables per the research report §4.3, encrypted, queryable through Athena,
with the Glue role PR-07 will use, built by an idempotent deploy script and verified live.

## Acceptance

- `--verify` reads back encryption, format-version, columns, partitions, an Athena query per table,
  retention, and the Glue role's grants in both directions; all pass.
- Offline tests pin specs, policies, idempotency and teardown symmetry.
