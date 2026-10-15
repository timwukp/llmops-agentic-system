# Intent: a sweep that called stage_complete is heard, and one that fails is seen

- **Slug:** sweep-unheard-calls
- **Author:** Claude (AI agent)
- **Date:** 2026-10-15
- **Accepted-by:** Tim WU
- **Status:** accepted

Accepted on the owner's request of 2026-10-15 (chat): establish why the 10-06 and 10-07 scheduled
sweeps never serviced a `stage_complete`, find the cause in the driver and/or the monitor prompt, and
fix it with offline tests through this chain, one PR on `main`.

## Problem

Read-only investigation (`deploy/evidence/VERIFICATION_sweep_unheard_calls.md`):

1. Four scheduled sweeps never completed, not two: 2026-08-18, 08-31, 10-06, 10-07. Each wrote its
   report (`monitoring/sweeps/sweep-<date>.json`) and each settled `UnclaimedStageOutputs` after
   2 re-asks + 1 evidence re-ask. No deadline cut, no stream death, no infra error; the
   self-reinvoke chain worked.
2. The agent DID try to call `stage_complete`, every time:
   * 10-06 / 10-07: it made the native call, the harness itself answered `Unknown tool:
     stage_complete` (runtime log: `available_tools=<['shell', 'skills']>`), the agent then ran
     `shell` (writing a `*.stage_complete.json`, echoing `STAGE_COMPLETE:`) and typed
     `stage_complete({...})` into its reply. `_drain` keeps only the LAST toolUse block, so the
     driver saw `tool=shell`; and the typed-call recovery runs only `if not tu`, so the shell block
     also switched off the parser that recognises `stage_complete({...})`.
   * 08-18 / 08-31: it typed `<inline-function name="stage_complete">{json}</inline-function>`, a
     spelling `parse_typed_call` does not know.
3. The failure was silent. The driver emitted `PipelineFailed`; the only rule on `llmops-pipeline`
   matches `EscalatedToHuman`. The `sweep#sweep` row still reads `invoked`. No SNS.
4. Since 2026-09-22 every completed sweep depended on the driver's end_turn recovery (33 of the 56
   serviced sweep turns on record) — the inline function is routinely answered `Unknown tool`
   inside the harness, and the sweep survives only when the failed call happens to be the turn's
   last block.

## Desired outcome

- A serviced inline function the agent called in a turn is serviced even when a harness-run tool
  followed it, and a typed call is recovered even when the turn also ran shell.
- The observed fourth spelling is recovered, under the same bar as the Python spelling.
- A scheduled sweep that does not complete says so: its outcome row is settled with the driver's
  verdict and the owner is notified.
- The monitor prompt tells the agent that a file or an echo is not the call, and what to do when the
  call errors.

## Acceptance

- Offline tests replay the live stream shapes of 10-06/10-07 and the live text of 08-31 and show
  each settling `completed`; a failed sweep updates its row and publishes once; a stage with a task
  token or an escalated sweep does not double-notify.
- Live: deploy the driver + monitor harness and run one sweep through `llmops-monitor-sweep`, only on
  the owner's go-ahead.
