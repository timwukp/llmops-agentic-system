# Spec: orphan memory cleanup record

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. Evidence: append a "Deletion, 2026-10-14" section: the pre-delete gate (each target matched one of
   the 7 agent prefixes, was referenced by no harness, ACTIVE, 0 actors, 0 records; all-or-nothing),
   the 7 `DeleteMemory` calls, the post-check (gone from ListMemories; all 7 harnesses READY on
   `llmops_shared_memory`; the effort-probe memory still present).
2. PROJECT_STATE: replace the open item with the remaining one (`llmops_effort_probe`).
3. AGENTS.md, CHANGELOG, TEST_RESULTS EN + zh-TW: past tense for the 7.
