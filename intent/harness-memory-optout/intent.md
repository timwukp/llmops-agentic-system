# Intent: create every harness with an explicit memory, never the managed default

- **Slug:** harness-memory-optout
- **Author:** Claude (AI agent)
- **Date:** 2026-10-13
- **Accepted-by:** Tim WU
- **Status:** accepted

Accepted on the owner's request of 2026-10-13 (chat), which named the three tasks below and the
proposed remedy (`memory={"disabled": {}}` at create).

## Problem

A live probe on 2026-10-13 found that `CreateHarness` WITHOUT a `memory` field provisions MANAGED
memory (it calls `CreateMemory` on `memory/<harnessName>-*`), and reached READY only with
`memory={"disabled": {}}`. Every `agents/*/harness.json` says it is "Created WITHOUT memory" and
`deploy/05_harnesses.py` sends none, so a fresh create races a default that the repo believes does
not exist. A read-only audit the same day showed the default is not new: each of the seven platform
harnesses left an extra managed memory at create time that the BYO wiring replaced but never removed.

## Desired outcome

- No create path in this repo can omit `memory`; the platform configs declare the opt-out.
- A create that comes back with managed memory anyway fails the deploy instead of passing.
- The stale `_memory_note`s and the AGENTS.md gotcha say what is live, guarded by tests derived
  from the configs and the code rather than a hand-copied claim.

## Acceptance

- Audit recorded in `deploy/evidence/`, redacted.
- Offline tests cover: the configs, every `create_harness(` call site, the create path's refusal
  and its read-back, `validate_config.py`'s handling of the union, and the doc claims.
- No AWS write. The 7 orphaned memories are reported, not deleted (owner's decision).
