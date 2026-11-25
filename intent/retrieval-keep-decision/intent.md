# Intent: record that llmops-retrieval is kept

- **Slug:** retrieval-keep-decision
- **Author:** Claude (AI agent)
- **Date:** 2026-11-24
- **Accepted-by:** Tim WU
- **Status:** accepted

Accepted on the owner's request of 2026-11-24 (chat): "保留collection", then "Yes, record it".

## Problem

The owner decided to keep the `llmops-retrieval` collection (and KB `llmops-org-facts`), which bills
~$11.52/day or more while it exists. The repo still calls that an open decision in
`PROJECT_STATE.md`, in evidence §5, and in CHANGELOG. And `deploy/README.md`'s 09 row says to tear
down after the eval, which is no longer what is happening for this one.

## Acceptance

- PROJECT_STATE (resource paragraph and the standing-cost posture), evidence §5 and the README 09 row
  say the collection is kept by owner decision of 2026-11-24, and keep the teardown command for when
  that changes.
- A CHANGELOG bullet records the decision; earlier entries are not rewritten.
- Chain `retrieval-network-e2e` set to `shipped` (#198).
