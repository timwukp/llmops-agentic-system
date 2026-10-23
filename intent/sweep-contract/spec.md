# Spec: sweep contract

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. `pipeline/contracts/sweep.schema.json` (`schema_version: "sweep/1"`). Required, with these names:
   `run_id`, `stage`="monitor", `task`="sweep", `region`, `generated_at` (UTC ISO-8601 Z), `idle_rule`
   {metric, window_hours, threshold}, `endpoints` {total, ours[], other_projects[], untagged[],
   idle_flagged[], hourly_cost_usd|null}, `orphan_endpoint_configs[]` and `orphan_models[]` {name,
   creation_time, owning_run|null, classification}, `agentcore` {memories_total, harnesses_total,
   llmops_harnesses_inspected, memory_orphans[] {id, createdAt, status}, memory_orphans_count,
   other_project_memories_count, contents_read=false}, `permission_gaps[]`, `deletions_performed`=0,
   `open_findings[]` of strings. Extra keys allowed (the agent may add detail); these may not vary.
2. `pipeline/contracts/sweep_contract.py`: a dependency-free validator (type, enum, pattern, minimum,
   required, properties, items, additionalProperties) and `check(doc, run_id)` adding cross-field
   rules: `run_id` matches the dispatch; `endpoints.total` = |ours|+|other_projects|+|untagged|;
   `memory_orphans_count` = |memory_orphans|; any null count requires a non-empty `permission_gaps`.
3. Driver: in `handle_stage_complete`, for (monitor, sweep), after `verify_outputs`, read the sweep.json
   (`params.sweep_uri`, else the output ending `sweep.json` or under `/sweeps/`) and run `check`;
   failures return `{"ok": False, "invalid_outputs": [...]}`; the loop answers the call "rejected" with
   the errors (capped) and asks for a rewrite. Bundle: `sweep_contract` imported in the fallback block
   (auto-vendored); `sweep.schema.json` in `VENDOR_DATA`.
4. Prompt: the sweep clause states the contract and `agentcore.memory_orphans`.
5. Tests, CHANGELOG, TEST_RESULTS EN + zh-TW counts, redaction-scanner counts.
