# Intent: spike S7's Unified Studio part

- **Slug:** spike-7-domain
- **Author:** Claude (AI agent)
- **Date:** 2026-10-14
- **Accepted-by:** Tim WU
- **Status:** accepted

## Problem

Phase 1 PR-05 (Unified Studio) is gated on S7's `smus_*` criteria: is a lakehouse table visible in a
Unified Studio project, does Glue lineage reach the domain, what does it cost. The owner asked to run it
(2026-10-14) in the account's existing domain, adding one spike project and touching nothing else.

## Desired outcome

The three criteria measured live, with every finding recorded, and nothing left behind.

## Acceptance

- Evidence names each run, what stopped it and what changed; teardown verified.
- Offline tests cover the domain part; the runner grant guard cannot be blinded by a non-literal client.
