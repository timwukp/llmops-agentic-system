# Intent: record the live deploy of the settle-and-teardown change

- **Slug:** deploy-settle-and-teardown-live
- **Author:** Claude (AI agent)
- **Date:** 2026-10-21
- **Accepted-by:** Tim WU
- **Status:** accepted

Accepted on the owner's request of 2026-10-21 (chat): record the driver and state-machine deploy of
#176 in a small PR, placed where the coordinator's merge order puts it.

## Problem

#176 (`intent/deploy-settle-and-teardown`) merged as `ecc2e50` and was deployed the same day on the
owner's go-ahead. The repo still describes it as undeployed. The evidence file says "Nothing is
deployed yet", and PROJECT_STATE names the 2026-10-16 probe/full-handoff deploy as the live driver
and a 36-state machine.

## Desired outcome

The evidence file, PROJECT_STATE, AGENTS.md and CHANGELOG record what was deployed, from which
tree, how it was read back, and what has not been exercised live yet.

## Acceptance

Every claim about the deploy matches what was measured on 2026-10-21. Docs and evidence only: no
code, ASL, IAM or harness change, and no deploy.
