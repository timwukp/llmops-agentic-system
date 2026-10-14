# Intent: reproducible lakehouse sources with countable defects

- **Slug:** lakehouse-generator
- **Author:** Claude (AI agent)
- **Date:** 2026-10-14
- **Accepted-by:** Tim WU
- **Status:** accepted

## Problem

Phase 1's Silver/Gold rules (PR-07) need input whose defects are known exactly, or a test cannot
tell a correct rule from a wrong one. The research report (§2, §4.1) specifies a seeded generator for
the five sources with planted defects; PR-06 of the Phase 1 plan builds it. The owner asked to open
PR-06 after #158 merged (2026-10-14).

## Desired outcome

A deterministic generator whose output matches the deployed Bronze schemas and whose planted defects
are listed with exact counts and ids.

## Acceptance

- Same seed → byte-identical files on two different machines.
- Every planted defect re-detected by an independent oracle, in both directions; every row valid
  against `deploy/10_lakehouse.py`'s schemas.
