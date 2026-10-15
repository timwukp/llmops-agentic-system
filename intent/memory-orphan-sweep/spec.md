# Spec: memory orphans in the sweep

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. `agents/monitor/harness.json` sweep clause: run `aws bedrock-agentcore-control list-memories` and
   `list-harnesses`, then `get-harness` on each `llmops_*` harness only; report as ORPHAN every
   memory whose id starts with `harness_llmops_` or `llmops_` and that no llmops harness's live
   `memory` references (id, createdAt, status); list memories named like neither as "another
   project's, not assessed". Do not read memory contents, do not delete. A CLI too old for these
   commands, or a denial, goes in permission_gaps.
2. `deploy/iam/harness_execution_role.json`: new `AgentCoreInventoryForSweep`
   (`ListMemories`, `ListHarnesses` on `*`) and `AgentCoreHarnessReadOwn` (`GetHarness` on
   `harness/llmops_*`). No `GetMemory`, `ListMemoryRecords`, `ListActors`, `DeleteMemory`.
   Action names validated with Access Analyzer `ValidatePolicy` (a misspelt control was flagged).
3. `tests/test_orchestration.py` `_CLI_TO_IAM`: map the three commands.
   New guards in `tests/test_harness_memory.py`: the sweep clause names the three commands and the
   two name prefixes; the grants are as in 2 and grant nothing that reads contents or mutates.
4. AGENTS.md: drop "deleted", say re-pointed; add the sweep as the detector.
5. CHANGELOG; TEST_RESULTS EN + zh-TW counts; redaction-scanner counts if they move.
