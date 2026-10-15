# Intent: a training extract you can prove

- **Slug:** gold-export
- **Author:** Claude (AI agent)
- **Date:** 2026-10-15
- **Accepted-by:** Tim WU
- **Status:** accepted

## Problem

Training must not read Iceberg directly (research report §4.4: snapshots expire, an isolated job may
have no route to `s3tables`, Macie cannot scan a table bucket). It needs a file pinned to one Gold
snapshot and named by its sha256, and the pipeline must be able to check that claim rather than trust
it. The owner asked to continue with PR-08 after #162 (2026-10-15).

## Desired outcome

A deterministic Gold → messages-JSONL export with its sha256 recorded in `gold.dataset_manifest`, a
driver attestation that recomputes the digest from the bytes, and Macie coverage of the extracts.

## Acceptance

- Two exports of one snapshot produce the same sha256; the driver re-verifies it from S3.
- The driver reads only its own extracts prefix; the export's SQL refuses what it would have to quote.
- Macie coverage of `lakehouse/extracts/` is reported (and created only under `--enable-pii-scan`).
