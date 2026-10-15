# VERIFICATION — memory-orphan sweep, deployed and run once (2026-10-15, us-east-1)

- **Change:** #164 (sweep clause in `agents/monitor/harness.json`; `AgentCoreInventoryForSweep` and
  `AgentCoreHarnessReadOwn` in `deploy/iam/harness_execution_role.json`).
- **Verdict:** PASS — the sweep ran through the pipeline, reported 0 orphans, which is the true state,
  read no memory contents, deleted nothing and filed no permission gap.

## 1. Pre-deploy (read-only)

`main` after #164's merge had exactly the tree that was reviewed (tree-sha equality, nothing else had
landed). Then:

| Check | Result |
|---|---|
| `01_iam.py --dry-run` (no `--memory-id`) | only `llmops-harness-execution` changes: +2 statements; the other 9 roles "no change". Identical before and after the merge |
| `01_iam.py --dry-run --memory-id <shared>` | would ALSO add `BYOMemoryEvents`/`BYOMemoryRetrieval`, which the live role does not carry (shared-memory access comes from `04_wire_memory.py`'s separate `SharedMemoryAccess` policy). Not used — outside this change |
| `05_harnesses.harness_config_drift(sent, live)` for monitor | only `systemPrompt` differs, and the tree prompt equals the live one plus exactly the new clause |
| Policy size after comment stripping | 8,228 of 10,240 characters |

## 2. Deploy

Run by the owner in their terminal; the agent's own `01_iam.py` apply was refused by the session's
permission classifier ("Protected-Scope IaC Apply"), and the agent did not work around it.

1. `deploy/01_iam.py --region us-east-1` → `"changed": true`; the live inline policy now has 29
   statements including both new Sids.
2. `deploy/05_harnesses.py --region us-east-1 --agent monitor` → `action: updated`,
   `config_confirmed: true`, `harness_version: 14`, `warmed: true`, warm turns 36.61 / 35.65 / 4.89 /
   4.77 s (two cold, two fast: the shape `warm()` documents).

## 3. IAM as the runtime principal

`simulate_principal_policy` with `PolicySourceArn = llmops-harness-execution` — not the deployer's own
credentials:

| Action | Resource | Decision | Wanted |
|---|---|---|---|
| ListMemories | `*` | allowed | allowed |
| ListHarnesses | `*` | allowed | allowed |
| GetHarness | `harness/llmops_monitor-*` | allowed | allowed |
| GetHarness | `harness/llmops_effort_probe-*` | allowed | allowed |
| GetHarness | `harness/finance_trading_assistant-*` (another project) | implicitDeny | implicitDeny |
| ListMemoryRecords | another project's memory | implicitDeny | implicitDeny |
| GetMemory | `harness_llmops_effort_probe_*` | implicitDeny | implicitDeny |
| DeleteMemory | `llmops_shared_memory-*` | implicitDeny | implicitDeny |
| UpdateHarness | `harness/llmops_monitor-*` | implicitDeny | implicitDeny |
| ListMemoryRecords | `llmops_shared_memory-*` (unchanged `SharedMemoryAccess`) | allowed | allowed |

**10/10.**

## 4. The sweep, through the pipeline

`lambda:Invoke llmops-monitor-sweep` (async, as the scheduler does) with
`run_id = sweep-2026-10-15-memory-verify`, so it shares no session with the day's scheduled sweep.
The monitor-sweep Lambda's timeout is 60 s, so its `sync` mode cannot carry a sweep; async is the only
mode that works. Driver → `llmops_monitor` v14 → `monitoring/sweeps/sweep-2026-10-15-memory-verify.json`
written at 05:19:30Z (6,301 bytes), `stage_complete` at 05:24:57Z.

The sweep's `memory_sweep` block, cross-checked against an independent ListMemories taken right after:

| Field | Sweep reported | Independent check |
|---|---|---|
| memories_total | 17 | 17 (24 before the 2026-10-14 deletion, minus 7) |
| llmops-named memories | `llmops_shared_memory-*` (referenced by all 7 platform harnesses), `harness_llmops_effort_probe_*` (referenced by `llmops_effort_probe`, managed) | the same two |
| memory_orphans | `[]`, count 0 | 0: both are referenced |
| other projects' memories | 15, "NOT ASSESSED, not orphans" | 17 − 2 = 15 |
| harnesses_total / llmops inspected | 18 / 8 | 18 / 8 (7 platform + effort probe) |
| contents_read | false | the role cannot (§3) |
| permission_gaps | `[]` | — |
| deletions_performed | 0 | memory count unchanged |

Observed, not ours to judge: 6 of the 15 other-project memories are named `harness_<name>_<4hex>-*`,
the managed-memory pattern, next to a BYO memory per assistant — the same residue shape the 7 deleted
llmops orphans had. The sweep correctly did not call them orphans; whether they are is that project's
question.

## 5. Turn protocol

3 turns, 2 ended in prose (`end_turn` after `shell`), the driver re-asked twice, the third turn called
`stage_complete`. Not introduced by this change — the previous scheduled sweeps:

| Sweep | turns | prose | serviced |
|---|---|---|---|
| 2026-10-14 | 5 | 2 | 2 |
| 2026-10-13 | 3 | 2 | 1 |
| 2026-10-12 / 11 / 10 / 08 | 1 | 0 | 1 |
| 2026-10-09 | 2 | 1 | 1 |
| 2026-10-07 / 06 | 4 | 4 | **0** |
| 2026-10-05 | 4 | 3 | 1 |

The 10-06 and 10-07 sweeps never serviced a `stage_complete`: those two days' sweeps did not complete.
That is a pre-existing defect outside this change and is recorded here so it is not lost.
