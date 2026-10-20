# Spec: a rejected stage_complete is heard, and a failed Deploy is torn down

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

Signed off under the same 2026-10-16 instruction as the intent, which asked for the fix end to end.
The owner reviews this spec in the PR.

## Requirements

1. In `_run_stage`, the end_turn override (a serviced tool with `stop_reason != "tool_use"`)
   replaces the call's `toolUseId` with `None` and marks it `recovered: "ended_turn"`, unless the
   shadowed recovery has already marked it. Every reply to the call then goes as text: the
   rejection branches' `messages`, the continuation carried across a self-reinvoke, and
   `_ack_terminal`.
2. `_tool_result_content` answers an `ended_turn` call as one plain user message. The message
   says the turn ended without a result, that an `Unknown tool` error was not the answer, and
   that the driver received and serviced the call. It contains no `toolUse` or `toolResult` block.
3. A `stage_complete` rejected for missing outputs:
   * logs one driver line naming the stage, the task and the missing URIs;
   * names every missing URI that ends in `/` as an S3 prefix that `head_object` cannot verify,
     and tells the agent to list the objects under it instead. The driver role holds
     `s3:HeadObject` only, with no `ListBucket`, so a prefix cannot be verified.
4. ASL: Deploy's `States.ALL` Catch goes to a new Task state, `TeardownAfterDeployFailed`. It
   dispatches `deploy/teardown` exactly as `Teardown` does, with `ResultPath: $.teardown`, the
   same `TimeoutSeconds`, a `States.ALL` Catch (`ResultPath: $.teardown_error`) to `EscalateFail`,
   and `Next: EscalateFail`. A failed Deploy therefore still ends `FAILED`. `Teardown` itself is
   unchanged.
5. Guards (offline):
   * a replay of the live deploy shape settles `completed` (requirements 1–3), and its reply
     carries no `toolResult`;
   * an end_turn `stage_complete` that succeeds is acked as text, not as a pair;
   * every exit (Next and every Catch) from each state that dispatches `deploy/deploy`,
     `deploy/smoke` or `monitor/health` reaches a state that dispatches `deploy/teardown` before
     any terminal state. States are found by their payload, not by name;
   * every path out of Deploy's Catch ends at `Fail` and never visits `MonitorReport` or
     `Complete`.
6. Negative controls, each asserted to fail its guard:
   * the end_turn override keeps the `toolUseId`;
   * the prefix hint is dropped;
   * Deploy's Catch goes straight to `EscalateFail`;
   * `TeardownAfterDeployFailed` continues to `MonitorReport`.
7. Docs: AGENTS.md gotcha (end_turn calls are answered as text; a failed Deploy is torn down);
   the evidence file; CHANGELOG; ARCHITECTURE EN + zh-TW; TEST_RESULTS EN + zh-TW; PROJECT_STATE
   state count. Every doc count that a guard derives is re-derived.
