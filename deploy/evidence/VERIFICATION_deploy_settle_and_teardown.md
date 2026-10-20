# VERIFICATION — a rejected stage_complete is heard, and a failed Deploy is torn down (2026-10-16)

The subject is `run-20261016T012010Z-9de45cbf` (`lakehouse` mode), dispatched for the
probe/full-handoff verification. Its eval gate passed. Deploy then created the endpoint
`llmops-student-run-20261016T012010Z-9de45cbf-i0` (ml.g5.xlarge, InService 02:14Z), never settled,
and escalated at 03:08:57Z: `EscalateFail` → `MarkRunFailed` → `FAILED`.

Everything below was read-only. Sources: the driver log `/aws/lambda/llmops-harness-driver`
(02:09–03:10Z), the deploy runtime log, `llmops-stage-events` for the run, the run's S3 prefix, and
`DescribeEndpoint` / `ListEndpoints`. Identifiers are redacted.

## Endpoint state, checked first

At the time of this investigation, `DescribeEndpoint` returned `Could not find endpoint`. `ListEndpoints
--name-contains llmops-student`, `ListEndpointConfigs` and `ListModels` (`--name-contains 9de45cbf`)
returned nothing. Nothing from the run is billing. The earlier record
(`VERIFICATION_probe_full_handoff.md`) attributes the delete to an AgentCore session on
`llmops-harness-execution` at 03:12:07Z (CloudTrail), so the endpoint billed ~58 min.

## Defect 1: why a serviced stage_complete did not settle

**The evidence record undercounted the attempts.** The driver serviced `stage_complete` in the
deploy stage **~25 times** (02:25:31–02:39:09Z, and once more at 03:03:13Z), not twice. Every
attempt had the same shape:

```
turn stage=deploy task=deploy stop_reason=end_turn tool=stage_complete ...
stage_complete arrived with stop_reason=end_turn; servicing it anyway ...
stream died again (1/10) ...: EventStreamError: ... (runtimeClientError) ...
    Inline function callback has no pending handoff in this session.
```

`llmops-stage-events` has no deploy `stage_complete` row. It has `escalated` at 03:08:57Z, and the
protocol row shows `last_tool: escalate_human`. `handle_stage_complete` records its event only after
`verify_outputs` passes, so every attempt was rejected before it recorded anything.

**What was rejected.** The deploy runtime log shows the call's arguments. `outputs` named five URIs.
Four exist: `serving.properties`, `pending_endpoint.json`, `progress.json` and `manifest.json`. The
fifth was `s3://<bucket>/runs/<run>/deployment/model/merged/`, which is a **prefix**. The objects
under it exist (`config.json`, `model.safetensors` of 3,441,185,608 bytes, and the tokenizer files),
but `verify_outputs` calls `head_object` on each URI, and `head_object` on a prefix always misses.
The rejection itself was correct. The driver role holds `s3:HeadObject` and no `ListBucket`, so it
cannot look inside a prefix.

**Why the rejection never reached the agent.** The harness had already answered the call
`Unknown tool`, and the agent ended the turn. The end_turn override serviced the call but kept its
`toolUseId`, so `_tool_result_content` built an echo plus a `toolResult` for an id the runtime had
already answered. With the turn closed there is no pending call to attach a result to, and the
runtime refused it. That refusal arrives as a stream error. The salvage path then sends "The stream
was interrupted. Continue from where you left off; call your pending inline function." That message
does not mention the rejection, so the agent re-sent the identical claim. Because `_out_of_time`
hands off whenever less than 850 s remains, the rejection was also carried across a self-reinvoke
as `_continuation` and died again in the next invocation.

The agent's own account at 03:08:57Z: `stage_complete` "unregistered in this environment ('Unknown
tool' on 25+ emissions across sessions)". From inside the session, that is what it looked like.

**Why this never showed up before.** `_ack_terminal` sends the same echo plus `toolResult` on the
success path and swallows the failure (its docstring flagged exactly this as untested). The 33
serviced end_turn sweep turns succeeded, so their refused acks were invisible. A rejection is not
an ack. It becomes the next turn's input.

### Fix

- The end_turn override marks the call `recovered: "ended_turn"` with no `toolUseId`, and
  `_tool_result_content` answers it as text. This covers rejections, the continuation, and the ack.
- A rejected `stage_complete` is logged (`stage_complete REJECTED stage=… task=…: missing […]`), and
  a missing URI ending in `/` is named as an S3 prefix, with the instruction to list the objects
  under it.

## Defect 2: a failed Deploy stranded its endpoint

Deploy's only Catch went to `EscalateFail`, which bypasses `Teardown`. It now goes to
`TeardownAfterDeployFailed`, which dispatches the same `deploy/teardown` task and then continues
to `EscalateFail`, so the run still ends `FAILED`. Reusing `Teardown` would have led to
`MonitorReport` and reported a failed deploy as a finished run.

## Offline verification

| Guard | Negative control |
|---|---|
| `test_a_rejected_end_turn_stage_complete_reaches_the_agent_and_the_stage_settles`: replays the live shape. The fake refuses a `toolResult` the way the runtime did and repeats its prefix claim unless told why. | m374 (the override keeps `toolUseId`), m375 (no prefix hint) |
| `test_a_succeeding_end_turn_stage_complete_is_acked_as_text` | m374 |
| `test_a_rejected_claim_names_a_prefix_as_a_prefix` | m375 |
| `test_every_exit_from_a_live_endpoint_state_reaches_a_teardown`: every exit from a state dispatching `deploy/deploy`, `deploy/smoke` or `monitor/health` reaches a `deploy/teardown` task. Found by payload, not by name. Red on the old ASL, naming `('Deploy', 'EscalateFail')`. | m376 |
| `test_a_failed_deploy_is_torn_down_and_still_fails_the_run` | m376, m377 (the failure teardown continues to `MonitorReport`) |

The new `ended_turn` marking also catches a shadowed call that lost its own marking, so the
first full negative-control run had one uncaught case: **407/408**. That case was m348, which
drops the shadowed classification. The reply still went as text, and the guard asserted only
the words "unknown tool", which both wordings contain. The guard now asserts the shadowed
wording, and m348 is caught again.

Results in this sandbox:

- **Unit tests:** 2096 of 2098 pass. Both failures are environmental and also fail on the
  untouched base commit:
  - `test_a_stream_that_goes_quiet_at_the_wall_still_hands_off` binds a local socket, which the
    sandbox refuses;
  - `test_capacity_race_guard.sh` uses `mktemp` into the macOS per-user temp directory, which the
    sandbox also refuses.
- **Negative controls:** 408/408 after the m348 fix. The two controls that name those
  environmental tests (`test_the_shell_suite_is_documented_with_its_assertion_count`, and
  `test_a_stream_that_goes_quiet_at_the_wall_still_hands_off`) read PASS here without proving
  anything. Neither guards code in this change. CI runs both for real.

Totals are in `docs/TEST_RESULTS.md`.

## Not done

- **Live.** Nothing is deployed yet. Live equals `main` at #175 (the probe/full handoff) for the
  driver and the state machine. This change deploys after it merges, from a fresh `origin/main`,
  with `07_lambdas.py --only driver --only state_machine` and the owner's go-ahead.
- **A separate defect, seen and not fixed:** for this run, `llmops-stage-events` holds 34
  `EscalationResolved` directives from the orchestrator between 03:12:12 and 03:30:34Z, several
  of them labelled "FINAL RESOLUTION". The escalation triage looped.
