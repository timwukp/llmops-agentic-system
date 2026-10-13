# Intent: a harness for the Phase 0 spikes

- **Slug:** lakehouse-spikes
- **Author:** Claude (AI agent)
- **Date:** 2026-10-13
- **Accepted-by:** Tim WU
- **Status:** accepted

## Problem

Phase 1 of the lakehouse build is gated on seven questions the AWS documentation cannot settle
(report §10/§13). Answering them means creating real resources; without a common harness, each
spike would reinvent teardown, tagging and evidence, and one forgotten deleter leaves billable
resources running in the account.

## Desired outcome

A shared spike harness plus the seven spike definitions, with the critical-path spike S7
(lakehouse substrate) implemented, so spikes can run one PR at a time in critical-path order.

## Acceptance

- Criteria are declared before running; plan() is offline; every created resource is deleted even
  when run() crashes; evidence is redacted; account-level settings are never changed silently.
- Offline tests prove each of these.
