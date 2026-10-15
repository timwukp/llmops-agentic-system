# Intent: the daily sweep reports orphaned AgentCore memories

- **Slug:** memory-orphan-sweep
- **Author:** Claude (AI agent)
- **Date:** 2026-10-15
- **Accepted-by:** Tim WU
- **Status:** accepted

Accepted on the owner's request of 2026-10-15 (chat): "兩件都做" — both proposals: correct the
AGENTS.md overclaim from #157, and make the pipeline itself find orphaned memories.

## Problem

1. #157 added to AGENTS.md that "a harness deleted or re-pointed later leaves the same residue".
   Re-pointed is evidenced (the 7 deleted orphans); deleted is not, and may be false: the S6 spike
   deleted its harnesses and no memory remained.
2. The 7 orphans were found by a person calling AWS directly. The owner's standing rule is that work
   goes through the pipeline, and the residue recurs whenever a harness is re-pointed away from a
   managed memory. Nothing in the pipeline looks.

## Desired outcome

- AGENTS.md claims only what was observed.
- `llmops_monitor`'s scheduled `sweep` reports memories named like ours that no llmops harness
  references, with the least privilege that answers the question.

## Acceptance

- Offline guards: every command the sweep clause names maps to an action the role grants; the new
  read grant stays metadata-only and `GetHarness` stays scoped to `llmops_*`.
- Live: deploy and one sweep dispatched through `llmops-monitor-sweep` only with the owner's go-ahead.
