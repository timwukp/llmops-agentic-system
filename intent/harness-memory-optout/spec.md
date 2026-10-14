# Spec: explicit create-time memory

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. Every `agents/*/harness.json` declares `"memory": {"disabled": {}}`. The update path is unchanged:
   `memory` is not in `UPDATED_FIELDS`, so a redeploy never undoes `04_wire_memory.py`'s attach.
2. `deploy/create_harness.py` gains `create_time_memory(cfg, where)`: returns the declared memory if
   it has exactly one member of the union (`agentCoreMemoryConfiguration | managedMemoryConfiguration
   | disabled`); raises `SystemExit` when `memory` is absent or malformed. Called by both
   `create_harness.py main()` and `05_harnesses.py create_or_update()` before `create_harness`.
3. `05_harnesses.py`: after a CREATE reaches READY, if the live harness carries
   `managedMemoryConfiguration` and the create did not ask for it, raise `SystemExit` naming the
   orphan memory ARN. Update path untouched.
4. `tools/spikes/s6_vpc_harness.py` passes `memory={"disabled": {}}`.
5. `deploy/validate_config.py validate_memory`: absent `memory` is an error (absence = managed);
   exactly one union member; `disabled` and `managedMemoryConfiguration` accepted.
6. Docs: `_memory_note` in all 7 configs; AGENTS.md gotcha (both live name patterns, orphans,
   IAM pattern mismatch); `deploy/iam/execution_role_base.json` `_comment`;
   `agents/harness.json.template` `_comment`; CHANGELOG; PROJECT_STATE (orphans as an open item);
   TEST_RESULTS EN + zh-TW (counts + evidence row); evidence file.
7. Guards (offline): configs declare an accepted memory; each `_memory_note` quotes the declared
   value verbatim and names `04_wire_memory.py`; every `create_harness(` call in `deploy/` and
   `tools/` passes `memory=` or calls `create_time_memory` in the same function; AGENTS.md states
   the opt-out and the config count derived from the configs.

## Out of scope

Deleting the 7 orphan memories; removing the `ManagedMemoryEvents` IAM statement (the
`llmops_effort_probe` harness may still rely on it). Both are proposed in the PR, not done.
