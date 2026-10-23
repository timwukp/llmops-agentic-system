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

## Deploy (2026-10-21)

#176 merged as `ecc2e50`, whose tree (`ad057f0`) is exactly the PR's head tree. The deploy ran on
the owner's go-ahead, from a fresh detached checkout of `origin/main` at `ecc2e50`, in the slot the
coordinating session assigned (after #175's probe/full handoff, which was live and already on `main`).

**Before.** All checks were read-only and ran from that checkout.

- `tools/audit_drift.py`:
  - **state machine:** 2 findings, both this change: `TeardownAfterDeployFailed` absent live, and
    Deploy's `Catch` differs.
  - **driver bundle:** only `handler.py` differed, first at line 343 (this change, +2,989 bytes).
    Every other bundled file matched. The first pass could not compare the driver (`IncompleteRead`
    while downloading the live zip); the second pass did.
  - **IAM** (10), **harnesses** (7) and **Lambda configuration** (14): 0 drift.
  - **Other Lambdas**, not touched by this change: `start-pipeline`, `resurrector`, `webhook`,
    `monitor-sweep` and `finops-reconcile` bundle a stale `manifest.schema.json` and no `tasks.py`.
- **Executions:** `list-executions --status-filter RUNNING` on `llmops-pipeline` returned 0.
  Re-checked immediately before the deploy, a day after the first check: still 0, with `origin/main`
  still at `ecc2e50` and the live driver still at its 2026-10-16T01:16:59Z deploy.
- **Dry run:** `07_lambdas.py --only driver --only state_machine --dry-run`: ASL `OK`, no
  diagnostics.

**Deploy.** `07_lambdas.py --region us-east-1 --only driver --only state_machine`: exit 0.

| Target | Result |
|---|---|
| `llmops-harness-driver` | `updated`; LastModified 2026-10-21T02:02:03Z, `Active`, `Successful` |
| `llmops-pipeline` | `updated`, `definition_confirmed: true`, `states_live: 37` |

**Read-back.**

- **Live definition** (`DescribeStateMachine`): 37 states. Deploy's `Catch` →
  `TeardownAfterDeployFailed`, which dispatches `deploy`/`teardown` and continues to `EscalateFail`,
  with its own `Catch` → `EscalateFail`.
- **Driver bytes:** the drift audit's driver leg hit `IncompleteRead` again, so the live zip was
  downloaded directly and every file compared with `ecc2e50`. All seven match:

  | File | Size |
  |---|---|
  | `handler.py` | 224,790 B |
  | `conductor_tools.py` | 9,407 B |
  | `events.py` | 10,938 B |
  | `manifest.schema.json` | 8,891 B |
  | `report.py` | 17,678 B |
  | `task_tokens.py` | 2,844 B |
  | `tasks.py` | 10,553 B |

- **Drift audit afterwards:** 0 drift on the state machine, IAM, harnesses and Lambda
  configuration. The remaining Lambda-code findings are the five other Lambdas listed above.

**Superseded driver, 2026-10-23.** #177 (the sweep contract) redeployed the driver from `7f3d0ee` at
06:30:46Z. That tree keeps this change and adds the sweep check (54 lines in `handler.py`). It changed
neither this change's lines nor the state machine. Read back on 2026-10-23: live still has 37 states,
and Deploy's `Catch` still goes to `TeardownAfterDeployFailed`.

## Not done

- **Exercised live.** Deployed 2026-10-21 (see "Deploy (2026-10-21)" above), but no run has yet
  taken either new path: an `ended_turn` rejection, or a failed Deploy's teardown. The first run
  that hits one is the first live evidence.
- **A separate defect, seen and not fixed:** for this run, `llmops-stage-events` holds 34
  `EscalationResolved` directives from the orchestrator between 03:12:12 and 03:30:34Z, several
  of them labelled "FINAL RESOLUTION". The escalation triage looped.
