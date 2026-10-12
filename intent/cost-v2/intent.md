# Intent: rebuild the Cost tab as spend visibility plus budget governance

- **Slug:** cost-v2
- **Author:** Claude (AI agent)
- **Date:** 2026-10-12
- **Accepted-by:** Tim WU
- **Status:** accepted

## Problem

The console's Cost tab answered "what will a run cost" and "who approves it", but not "where
did the money go" or "what stops it". Live, its spend panel read $0.00: `llmops-cost-actuals`
held 164 rows and every one was `#audit#`, while 68 project training jobs had run. Its only
limit is advisory — an over-budget run is reported, then launched anyway.

## Desired outcome

1. Cost visibility: overview, usage, cost per task, breakdown and line-item detail, every
   figure labelled with its basis (attributed / estimate / billed / account-wide). Scope
   modelled on the Agent CI/CD Admin console's Cost page.
2. Cost governance: budgets with enforcement guardrails, modelled on the Token Usage
   Monitoring app's governance page (budget vs actual vs forecast, enforcement mode,
   guardrail posture, alert feed).

Decisions taken by the owner in session (2026-10-12): tiered enforcement (default notify,
switchable to block); "task" = a console Task (consultation) plus the runs it dispatched; one
Cost tab with sub-tabs; the old estimator/approval UI to be redone rather than kept as is.

## Acceptance

- The six sub-tabs render real data on the live portal.
- A `block` budget refuses a launch that would breach it; a `notify` budget records an alert
  and lets it proceed.
- In-flight hard-stop is explicitly out of scope (phase 2) and the UI says so.
