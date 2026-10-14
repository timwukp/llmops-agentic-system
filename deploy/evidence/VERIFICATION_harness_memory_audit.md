# VERIFICATION — harness memory audit (read-only)

- **Date:** 2026-10-13, us-east-1
- **Calls:** `ListHarnesses`, `GetHarness` (each), `ListMemories`, `GetMemory`, `ListActors`,
  `ListMemoryRecords` — read-only; nothing created, updated or deleted.
- **Trigger:** a probe the same day found that `CreateHarness` WITHOUT `memory` provisions MANAGED
  memory (`CreateMemory` on `memory/<harnessName>-*`) and reached READY only with
  `memory={"disabled": {}}`, while every `agents/*/harness.json` said "Created WITHOUT memory".

## 1. What each platform harness is attached to now

| Harness | Created (UTC) | Live `memory` | actorId |
|---|---|---|---|
| llmops_data_prep | 2026-07-28 12:56:23 | BYO `llmops_shared_memory-hbEZ9K8d57` | `llmops_data_prep` |
| llmops_finetune | 2026-07-28 23:38:20 | BYO, same | `llmops_finetune` |
| llmops_eval | 2026-07-29 02:43:46 | BYO, same | `llmops_eval` |
| llmops_deploy | 2026-07-29 02:46:25 | BYO, same | `llmops_deploy` |
| llmops_monitor | 2026-07-29 03:33:35 | BYO, same | `llmops_monitor` |
| llmops_orchestrator | 2026-07-29 08:52:00 | BYO, same | full harness id |
| llmops_finops | 2026-07-31 05:10:22 | BYO, same | full harness id |

All 7 are on the shared BYO memory. None is managed or disabled. (finops and orchestrator still
carry the pre-fix semantic retrieval of topK 10 / relevanceScore 0.2; the five workers have 5 / 0.6.
`04_wire_memory.py` already reports this; it is not new here.)

## 2. The extra memories they got at create

`ListMemories` returns one `harness_llmops_<agent>_<4hex>-<10>` memory per platform harness, each
created **within one second of its harness's `createdAt`**:

| Memory | Created (UTC) | Strategies | Event expiry | Actors | Records |
|---|---|---|---|---|---|
| harness_llmops_data_prep_214a-PhkL9cFlfH | 2026-07-28 12:56:23 | SEMANTIC, SUMMARIZATION | 30 d | 0 | 0 |
| harness_llmops_finetune_29ae-hCxr4lD8eB | 2026-07-28 23:38:20 | same | 30 d | 0 | 0 |
| harness_llmops_eval_4ecb-01m9aG68Is | 2026-07-29 02:43:47 | same | 30 d | 0 | 0 |
| harness_llmops_deploy_72b3-SFQwRSBttw | 2026-07-29 02:46:25 | same | 30 d | 0 | 0 |
| harness_llmops_monitor_af13-1Yl0pnDVy3 | 2026-07-29 03:33:35 | same | 30 d | 0 | 0 |
| harness_llmops_orchestrator_c0d7-TufsNF7Aru | 2026-07-29 08:52:01 | same | 30 d | 0 | 0 |
| harness_llmops_finops_b05d-WrnuZjElYA | 2026-07-31 05:10:23 | same | 30 d | 0 | 0 |

So the managed default is not new: every create in this repo has provisioned one. `04_wire_memory.py`
then replaced it with the BYO memory, and **UpdateHarness does not delete the memory it replaces**,
so all 7 are still ACTIVE. They hold nothing (0 actors; 0 records in every strategy namespace). Note
that 0 actors cannot rule out events written in the window between create and wire, because events
expire after 30 days. Semantic records do not expire, and there are none.

Outside the platform: `llmops_effort_probe` (a 2026-08-01 probe harness) is still on its managed
memory `harness_llmops_effort_probe_05cc-FR2mja5Td1`, which holds 4 semantic records under actor
`default`.

## 3. The name pattern changed

| Created | Pattern | Example in this account |
|---|---|---|
| through 2026-08-04 | `harness_<name>_<4hex>-<10>` | the 7 above, plus 6 non-llmops harnesses |
| from 2026-08-11 | `<harnessName>-<10>` | `PrismTheoryScout-leiWeL4hW0` (a non-llmops harness), and the 2026-10-13 probe |

`deploy/iam/harness_execution_role.json` grants `ManagedMemoryEvents` on `memory/harness_*`, which
matches only the old pattern.

## 4. Would a fresh create today provision managed memory before BYO is wired?

Yes. `05_harnesses.py` created with no `memory`; `04_wire_memory.py` exits "run 05_harnesses.py
first" for a harness that does not exist, so the real order is 05 then 04, and every fresh create
went through the managed default first. BYO at create is not available on a fresh deploy (04 creates
the memory and needs the harnesses), so the fix is the explicit opt-out at create:
`"memory": {"disabled": {}}` in each config, enforced by `create_time_memory()`.

**Not verified live:** a create with `{"disabled": {}}` followed by `04_wire_memory.py`'s
`UpdateHarness(memory={"optionalValue": {"agentCoreMemoryConfiguration": ...}})`. The audit was
read-only by instruction. `05_harnesses.py` now fails a create that comes back managed, so the first
real create will either confirm the opt-out or say so.

## Not done (owner's decision)

- ~~Delete the 7 empty orphan memories~~ — done 2026-10-14, below. `llmops_effort_probe` and its 4
  records remain undecided.
- Remove or re-scope `ManagedMemoryEvents` once nothing on the execution role uses managed memory.

## Deletion, 2026-10-14 (owner's request)

The only write in this file. The script re-checked every target immediately before deleting, and
deleted nothing unless all 7 passed:

| Check | Source | Result for all 7 |
|---|---|---|
| id matches `harness_llmops_<agent>_` for one of the 7 platform agents (not `effort_probe`) | ListMemories | 7 matched |
| referenced by no harness's live `memory` | GetHarness on every harness (11 memories referenced in total) | none referenced |
| status | GetMemory | ACTIVE |
| actors | ListActors | 0 |
| records, every strategy namespace | ListMemoryRecords | 0 |

Then `DeleteMemory` on each: `harness_llmops_{data_prep_214a,deploy_72b3,eval_4ecb,finetune_29ae,
finops_b05d,monitor_af13,orchestrator_c0d7}-*`. Post-check: none of the 7 is listed by ListMemories.
`llmops_shared_memory-hbEZ9K8d57` is present, all 7 platform harnesses are READY and attached to it,
and `harness_llmops_effort_probe_05cc-FR2mja5Td1` is still present and still attached to its probe.
