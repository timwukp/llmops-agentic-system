# Intent: a call refused every turn stops buying turns

- **Slug:** rejected-call-budget
- **Author:** Claude (AI agent)
- **Date:** 2026-10-16
- **Accepted-by:** Tim WU
- **Status:** accepted

Accepted as step 4 of ORDER v1. Tim approved that order on 2026-10-16, when he named a40836 coordinator.
The scope ("a consecutive-refusal budget plus a triage regression test, on top of 945982's change") is
the coordinator's summary of 1df6c6's review, not Tim's words. Tim confirmed the scope in chat on
2026-10-23, before this PR was pushed.

## Problem

`triage-run-20261016T012010Z-9de45cbf` re-sent `resolve_escalation` for a run that could not hear it
96 times in four hours (190 turns), until the driver was stopped by hand. 945982's `ended_turn`
change makes the refusal audible to the agent. Nothing bounds an agent that hears it and re-sends:
every serviced call re-arms the prose budget, and on 2026-08-01 a conductor told "undeliverable" did
exactly that.

## Acceptance

- Consecutive refused calls (rejected / undeliverable / unsupported) end the invocation at a budget;
  any accepted call resets it; the count survives a Lambda handoff.
- A triage that exhausts it is paged by the backstop; a stage settles under its own error name.
- The live loop replayed offline ends within the budget, with every reply in text.
