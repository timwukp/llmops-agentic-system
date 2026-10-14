# Intent: Bronze → Silver → Gold with rules that catch exactly what was planted

- **Slug:** lakehouse-medallion
- **Author:** Claude (AI agent)
- **Date:** 2026-10-14
- **Accepted-by:** Tim WU
- **Status:** accepted

## Problem

Phase 1 needs the medallion transforms between the storage PR-04 built and the generator PR-06 built.
The research report (§2) requires that each planted defect has a Silver/Gold rule that catches it and a
test that proves it. PR-04's Glue role grants were proved only by simulation and need a real write. The
owner asked to continue in the agreed order (2026-10-14) and to ship a read-only console Data tab right
after this PR.

## Desired outcome

Pure, tested transforms; thin Glue 5 jobs that run them; DQ rules; and a live run whose rule report
matches the generator's plants exactly.

## Acceptance

- Offline: every rule's catch equals the plant, in both directions, across seeds and scales.
- Live: the three jobs succeed as `llmops-lakehouse-glue`; the rule report equals the plants; Silver and
  Gold DQ pass; Athena row counts equal the reports.
