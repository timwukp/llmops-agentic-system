# Verification: why four scheduled sweeps never serviced a stage_complete

- **Date:** 2026-10-15
- **Change:** `intent/sweep-unheard-calls/`
- **Mode:** §1–4 read-only investigation (DynamoDB `llmops-stage-events`, CloudWatch
  `/aws/lambda/llmops-harness-driver` and the `llmops_monitor` runtime log group, S3 `monitoring/`,
  EventBridge rules). §5 offline. §6 deployed and verified live on the owner's go-ahead.

## 1. Which sweeps did not complete

Every `sweep-*` run in `llmops-stage-events`, read off the `protocol#monitor#sweep#e0` row
(turns / prose / serviced) and the terminal event row:

| Sweep | turns/prose/serviced | terminal row | report in S3 |
|---|---|---|---|
| 2026-08-18 | 4/4/0 | `UnclaimedStageOutputs` | yes |
| 2026-08-31 | 4/4/0 | `UnclaimedStageOutputs` | yes |
| 2026-10-06 | 4/4/0 | `UnclaimedStageOutputs` (`re_asks 2, evidence_reask 1, progress 0`, 4,709 B verified) | yes |
| 2026-10-07 | 4/4/0 | `UnclaimedStageOutputs` (same, 11,079 B verified) | yes |
| every other sweep 08-01 … 10-15 | — | `stage_complete` | yes |

So four sweeps failed, not the two first reported in
[VERIFICATION_memory_orphan_sweep.md §5](VERIFICATION_memory_orphan_sweep.md). In each one the agent did the work and wrote
`monitoring/sweeps/sweep-<date>.json`.

## 2. What the driver did (driver log, 10-06 and 10-07)

Identical on both days, three invocations each:

```
turn ... stop_reason=end_turn tool=shell re_asks=0 text_chars=2953
handing off ... between turns: 736266ms wall left, re_asks=1
turn ... stop_reason=end_turn tool=shell re_asks=1 text_chars=2413
turn ... stop_reason=end_turn tool=shell re_asks=2 text_chars=1590
prose budget spent, but 1 of this stage's claimed outputs are verified in S3 (4709 bytes); granting one evidence-backed turn
handing off ...
turn ... stop_reason=end_turn tool=shell re_asks=2 text_chars=715
1 output(s) for this stage are present in S3 ... but the agent never claimed them ... across 3 re-asks
```

- **Re-ask budget:** exhausted (2 prose re-asks + 1 evidence re-ask). This is the proximate cause.
- **Self-reinvoke chain:** worked; every handoff resumed.
- **Deadline cut / infra error:** none (`deadline_cuts 0`, `infra_error_turns_total 0`).

## 3. What the agent did (runtime log)

The agent called `stage_complete` in each of the last three turns. The harness answered the call
itself instead of yielding it to the driver:

```
tool_name=<stage_complete>, available_tools=<['shell', 'skills']> | tool not found in registry
toolResult: "Unknown tool: stage_complete"  (status: error)
```

After that error the agent improvised. It ran `shell` to upload `stage_complete.json` to
`monitoring/sweeps/` and `monitoring/events/`, then echoed `STAGE_COMPLETE: ...`. Its later turns
typed `stage_complete({...})` into the reply ("No CLI shim exists — the inline functions are parsed
from the reply text by the orchestrator"). Each turn ended `end_turn` with `shell` as its last block.

The driver missed it in two places:

1. `_drain` keeps one slot, the LAST toolUse block, so the real `stage_complete` block was
   overwritten by the `shell` block that followed it.
2. Typed-call recovery ran only `if not tu`. The shell block counts as a `tu`, so the parser never
   saw `stage_complete({...})`, a spelling it already recognises.

On 08-18 and 08-31 the agent made no native call. It typed
`<inline-function name="stage_complete">{json}</inline-function>` three times, a fourth spelling
`parse_typed_call` did not know.

The same registry error shows up on days that succeeded. 10-08 (a clean 1/0/1) logged it in its first
request. The sweep was serviced only because the failed `stage_complete` happened to be the turn's
last block, so the driver's end_turn override caught it. Across all 59 sweep protocol rows, 33 of
the 56 serviced turns were such recoveries. Every sweep since 09-22 depended on one.
`finops/reconcile` shows the same signature (16 of 93). Pipeline stages show none
(`recovered_ending_in_prose = 0`). Why the registry omits the inline functions is not established.
The operator principal cannot `GetHarness`, so the live tool config was not read.

## 4. Escalation and alerting

None:
- The driver emitted `PipelineFailed`. The only rule on `llmops-pipeline` is
  `llmops-escalation-triage`, which matches `EscalatedToHuman` only, so the event reached no one.
- No SNS publish; no `escalated` row.
- The `sweep#sweep` row for both days still reads `status: invoked`. The dispatcher writes it at
  async dispatch and nothing ever wrote it again.

## 5. Fix, verified offline

| Live failure | Test (replays the live shape) | Unfixed driver | Fixed |
|---|---|---|---|
| 10-06/07: call, then shell | `test_a_stage_complete_followed_by_shell_in_the_same_turn_is_serviced` | fails | passes |
| 10-06: shell, then typed `stage_complete({...})` | `test_a_call_typed_after_a_shell_block_is_recovered` | fails | passes |
| 08-18/31: `<inline-function>` spelling | `test_the_inline_function_tag_spelling_is_recovered` | fails | passes |
| silent failure | `test_a_sweep_that_does_not_complete_settles_its_row_and_pages_once` | fails | passes |

Full new set: 14 tests, 9 of which fail on the unfixed driver. The 5 that pass there are the guards
that must hold either way (prose stays prose, the dispatcher's row key, the prompt clause).

## 6. Deployed and verified live (2026-10-15)

`deploy/preflight.py` READY. Then:
- `deploy/07_lambdas.py --only driver --only monitor_sweep`: both `updated`, `Successful`, 08:16Z.
- `deploy/05_harnesses.py --agent monitor`: `llmops_monitor` v15, READY, config read back. Its warm-up
  turn did not run (sandbox egress), and was not retried: that is a direct InvokeHarness, and work is
  dispatched only through the pipeline.

**A fifth failure, on the old code, the same morning.** The scheduled `sweep-2026-10-15` (08:00Z,
before the deploy) ended `UnclaimedStageOutputs` 4/4/0 at 08:08:18Z, `sweep#sweep` left at `invoked`.
Its runtime log has every inline function answered `Unknown tool`: `stage_complete` 8 times,
`checkpoint` once, `escalate_human` once. Its last turn: "STAGE_COMPLETE emitted via the only
registered tool (shell)". So not even `escalate_human` can reach the driver natively while the
registry is in this state, which is the case for driver-side recovery rather than prompt-side.

**The sweep, through the pipeline.** `lambda:Invoke llmops-monitor-sweep` (async, as the scheduler does)
with `run_id = sweep-2026-10-15-unheard-calls-verify`, 08:19:16Z:

| Check | Result |
|---|---|
| `protocol#monitor#sweep#e0` | turns 1, serviced 1, prose 0, `recovered_ending_in_prose` 1, `recovered_shadowed_calls` 0. The new counter is present, so the new driver ran |
| `…#monitor#stage_complete` | 08:23:27Z, outputs `monitoring/sweeps/…verify.json` (7,133 B) + manifest, both verified |
| `sweep#sweep` | `status: completed`, `settled_at 2026-10-15T08:23:27Z`, `driver_outcome` set (was `invoked` forever before) |
| `*.stage_complete.json` in S3 | none written (the scheduled run on the old prompt wrote one at 08:06Z) |
| runtime log | `stage_complete` still answered `Unknown tool` in-harness. The agent then: "Per protocol, I will not re-send it through the shell, write it to S3, or retype it" and ended the turn |

What this proves and what it does not. Live, the run was saved by the **prompt clause** plus the
existing end_turn recovery: the agent stopped after the error, so the call was the last block. The
**shadowed-call path** (a harness tool after the call) and the **typed-call fixes** were not exercised
live (`recovered_shadowed_calls 0`, `recovered_typed_calls 0`); they are proven by the replay tests in
§5 only. The failure-page path (SNS on a non-completed sweep) was likewise not triggered, because the
sweep completed. The next scheduled sweeps are the live sample for both.
