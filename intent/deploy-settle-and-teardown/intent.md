# Intent: a rejected stage_complete is heard, and a failed Deploy is torn down

- **Slug:** deploy-settle-and-teardown
- **Author:** Claude (AI agent)
- **Date:** 2026-10-16
- **Accepted-by:** Tim WU
- **Status:** accepted

Accepted on the owner's request of 2026-10-16 (chat): find out why a serviced `stage_complete` in
the deploy stage of `run-20261016T012010Z-9de45cbf` did not settle the task token, fix it, and route
Deploy failures through a teardown the way SmokeTest's Catch does, with a guard and a negative
control. One PR on `main`.

## Problem

Read-only investigation (driver log `/aws/lambda/llmops-harness-driver` 02:10–03:09Z, the deploy
runtime log, `llmops-stage-events`, S3). Details are in
`deploy/evidence/VERIFICATION_deploy_settle_and_teardown.md`.

1. **The claim was rejected, correctly.** Every deploy `stage_complete` listed
   `s3://…/deployment/model/merged/` in `outputs`, which is a prefix and not an object. The
   other four outputs exist. `verify_outputs` calls `head_object` on each URI, so the prefix
   always shows up as missing. `handle_stage_complete` returned `ok: False` before writing
   anything, which is why `llmops-stage-events` has no deploy `stage_complete` row.
2. **The rejection never reached the agent.** The harness had already answered the call
   (`Unknown tool`) and the turn ended (`stop_reason=end_turn`). The end_turn override serviced
   the call but kept its `toolUseId`, so the rejection went back as an echo plus `toolResult` for
   an id the harness had already answered. The runtime refused it: `runtimeClientError: Inline
   function callback has no pending handoff in this session`. The driver treated that as a stream
   death, and the salvage prompt says only "call your pending inline function". So the agent
   re-sent the same claim, roughly 25 times between 02:25 and 02:39Z and again at 03:03Z, never
   learning that the prefix was the problem. At 03:08:57Z it escalated, reporting the tool as
   "unregistered".
3. **The success path has the same flaw but hides it.** `_ack_terminal` swallows the same refusal,
   which is why serviced end_turn calls that *succeeded* never surfaced it.
4. **A failed Deploy strands its endpoint.** Deploy's only Catch goes to `EscalateFail` →
   `MarkRunFailed` → `Fail`, which bypasses `Teardown`. The endpoint (ml.g5.xlarge) billed
   02:14–03:12Z. An escalation-triage agent session deleted it, not the state machine.

## Desired outcome

- A serviced inline function whose turn ended without `stop_reason=tool_use` gets every reply, a
  rejection or an ack, as plain text, because the runtime has no pending call to attach a
  `toolResult` to.
- A rejected claim says why. A prefix is named as a prefix, and the driver logs which outputs
  were missing.
- Any failure out of Deploy runs a teardown before the run escalates and fails. A failed Deploy
  still ends the run FAILED. It must not reach `MonitorReport` or `Complete`.

## Acceptance

- Offline: a replay of the live deploy turn shape, with a fake that refuses a `toolResult` the way
  the runtime did and repeats its claim unless told why, settles `completed`. Before the fix it
  does not.
- Offline: every exit from a state that can leave an endpoint alive reaches a `deploy/teardown`
  task, and every exit from Deploy's failure path ends in `Fail`. Each guard has a negative
  control that breaks it.
- Live: deploy and re-run on the owner's go-ahead only. Not part of this change.
