# Spec: hearing the call, seeing the failure

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

Signed off under the same 2026-10-15 instruction as the intent, which asked for the fix end to end;
the owner reviews this spec in the PR.

## Requirements

1. `_drain` returns `serviced_tool_use`: the last toolUse block whose name is in `SERVICED_TOOLS`,
   inputs parsed like `tool_use`. `tool_use` keeps its meaning (the last block of any kind).
2. In `_run_stage`, when the last block is NOT a serviced tool, `stop_reason != "tool_use"`, and
   `serviced_tool_use` is set, that call is serviced. Its `toolUseId` is dropped (the harness has
   already answered it, so echoing it back would duplicate an answered id after later messages) and
   its reply goes as plain text that says so. Counted in a new protocol counter
   `recovered_shadowed_calls`, as a subset of `recovered_ending_in_prose`.
3. Typed-call recovery runs when no serviced call is in hand — not only when no block arrived. A
   genuine serviced call still wins over a typed one; a shell block is not a competing call.
4. `parse_typed_call` recognises `<inline-function name="X">{...}</inline-function>`: the body must
   be exactly one JSON object followed by the closing tag; last call wins across all four spellings;
   `SERVICED_TOOLS` gates the name as before.
5. `monitor_sweep.build_payload` adds `outcome_sk` = the sk `record_outcome` writes. The driver, for
   an invocation with `outcome_sk` and no `task_token`, on every terminal return (and on a crash):
   * updates that `EVENTS_TABLE` row: `status`, `driver_outcome` (JSON, ≤ 4000 chars), `settled_at`;
   * publishes to `LLMOPS_SNS_TOPIC` unless the status is `completed` or `escalated`
     (`handle_escalate` already published).
   Each channel wrapped separately; neither can change the returned outcome. No IAM change: the
   driver role already holds `UpdateItem` on `llmops-stage-events` and `sns:Publish` on
   `llmops-escalations`.
6. `agents/monitor/harness.json` TURN-END INVARIANT: the inline functions are tool calls, not files,
   echoes or reply text; if one returns an error such as `Unknown tool`, make no further tool calls
   and end the turn.
7. AGENTS.md gotcha; evidence file; CHANGELOG; TEST_RESULTS EN + zh-TW counts and row.
