# VERIFICATION — probe and full job as two launch-and-release handoffs (2026-10-16)

## The defect, four times

The finetune `launch` task told the agent to run a 20-step probe, read its measured s/step and VRAM,
launch the full job, *and* "call job_launched and END YOUR TURN". That is one handoff for two jobs.
Every time it ran, the agent handed off on the probe:

| Run | What happened |
|---|---|
| `run-20261015T065307Z-97655783` | The probe's completion resumed `FinetuneAnalyze`. The full job was launched from analyze, analyze completed at once, and eval waited for the artifact. |
| `run-20261015T104050Z-c7a2c9d8` | `job_launched` named the probe. The agent kept working, and the token was parked after the probe had already finished (PR-11's `_deliver_if_already_terminal`). |
| `run-20261015T130703Z-f5d05255` | Analyze launched the full job and waited in-session through an ml.g5.2xlarge capacity queue until analyze's 3600 s `TimeoutSeconds` failed the run. |
| `run-20261015T142928Z-fbcd65dc` (PR-11's verification run, observed read-only) | The probe queued for capacity for ~8 h. `FinetuneAnalyze` was entered on its completion at 22:45:03Z, the full job was created at 22:46:27Z (inside analyze), and analyze held the session for 52 min until 23:37:25Z. It survived only because live analyze carried PR-11's 86400 s stopgap. |

## What changed

- `launch`/`remediate` hand off the probe, `FinetuneLaunchFull` (`launch_full`) hands off the full
  job, and `FinetuneAnalyze` is entered only from `FinetuneLaunchFull`, keeping a 3600 s ceiling.
- The driver checks every `job_launched` against `JOB_HANDOFFS`, reading the job's own `max_steps`
  (DescribeTrainingJob). A wrong kind, a nonexistent job or a task with no handoff is rejected into
  the turn.
- A probe's completion no longer announces `ModelTrained`.
- Offline: 2084 unit tests, and 430/430 negative controls (20 of the 363 mutations are new, covering
  31 guard pairs), on base `80a13c9`. After rebasing onto `94c5e5a` (#173, #174): 2123 tests, and
  433 pairs over 366 mutations.

## Deploy (2026-10-16)

Live was running PR-11's tree, not `main`. PR #173 was open, and its branch `b0adbb3` had been
deployed together with its uncommitted FinetuneAnalyze 86400 s stopgap. Deploying a `main`-based tree
would have removed PR-11 from live, so the deploy tree was `b0adbb3` plus this change's patch, which
applied cleanly to every code file.

Before deploying:
- The live driver and resume zips were downloaded. `handler.py` and the vendored contracts are
  byte-identical to `b0adbb3`, and their CodeSha256 values were unchanged at deploy time.
- `tools/audit_drift.py`: the only drift was this change (driver IAM; `FinetuneLaunchFull` absent;
  `FinetuneLaunch`/`RemediateFinetune`/`FinetuneAnalyze`; the finetune prompt), plus one pre-existing
  item (`llmops-start-pipeline` carries an older `manifest.schema.json`, not touched here).
- `01_iam.py --dry-run`: one pending statement, `HandoffJobClassification`.
- No execution was RUNNING. PR-11's run was left to finish first (owner's decision) and SUCCEEDED
  at 00:53:55Z.

| Step | Result |
|---|---|
| `01_iam.py` | **Not applied, and not needed:** the apply was refused by this session's permission policy. `simulate_principal_policy` as `llmops-lambda-driver`: `DescribeTrainingJob` **allowed** on `training-job/llmops-*` via #173's `LateJobDelivery`; `StopTrainingJob` / `CreateTrainingJob` **implicitDeny**. After the rebase onto #173, `main` carries that same statement, and the separate `HandoffJobClassification` statement was dropped as redundant: its negative control went uncaught, because removing it left the grant in place. `LateJobDelivery`'s comment now names the handoff check as its second reader, and the control strips the action from every driver statement. |
| `07_lambdas.py --only driver --only resume --only state_machine` (01:16:51Z) | driver and resume updated. State machine updated, `definition_confirmed`, `states_live: 36`. |
| `05_harnesses.py --agent finetune` (01:18:04Z) | `llmops_finetune` v16, READY, `config_confirmed`. |
| Read-back | Driver and resume code downloaded again: byte-identical to the deploy tree, `tasks.py` bundled. `audit_drift.py`: state machine 0 drift, harnesses 0 drift. |

## Live run: `run-20261016T012010Z-9de45cbf` (`lakehouse`)

Dispatched through the `llmops-start-pipeline` Lambda (`trigger_source:
probe-full-handoff-verification`), with the same parameters as PR-11's run:
- dataset `run-20261015t125141z-bcb61189`;
- student `Qwen/Qwen3-1.7B`;
- gates `{format_validity: 0.95, answer_match: 0.6}`.

The first attempt omitted `params.dataset` and was refused at dispatch, before anything was created,
which is that guard working.

**Three handoffs, in order** (UTC; Step Functions history, DescribeTrainingJob, run row):

| State | Entered → exited | Job it waited on | Job created | max_steps | Billed | Settle output |
|---|---|---|---|---|---|---|
| `FinetuneLaunch` | 01:24:54 → 01:34:33 | `…-i0-probe` | 01:27:56–01:28:00, inside this state | `20` | 285 s | `status: measured, job_kind: probe` |
| `FinetuneLaunchFull` | 01:34:33 → 01:46:54 | `…-i0` | 01:35:39, inside this state | `0` | 410 s | `status: trained, job_kind: full` |
| `FinetuneAnalyze` | 01:46:54 → 01:52:01 | none | — | — | — | `stage_complete` |

- **Analyze ran after the full job, and only then.** It was entered 4 s after the full job ended
  (01:46:50), took 5 min 7 s, launched nothing (the run has exactly two `llmops-qlora-*` jobs), and
  called `stage_complete`. Its evidence: 87 steps over 3 epochs, eval loss 0.295, merged model
  saved. Under the old protocol, the same state on PR-11's run held a session for 52 minutes.
- **The driver's check classified both jobs.** The run row read `job_kind: probe` while the probe
  ran and `job_kind: full` once `FinetuneLaunchFull` handed off. That field is written only after
  the driver has read the job's `max_steps`. No `job_launched` was rejected. Driver log lines:
  - `task=launch`: one turn, `tool=job_launched`;
  - `task=launch_full`: three turns (`shell`, `checkpoint`, then `job_launched`, with one stream
    salvage in between);
  - `task=analyze`: `tool=shell`, then `stage_complete`.
- **`launch_full` measured before launching.** It read the probe's `metrics.json` out of its
  `model.tar.gz`: `sec_per_step.p50_steady` 0.923 (the mean, 1.098, discarded, because the first
  step paid 4.19 s of CUDA autotune) and VRAM reserved fraction 0.101.
  - It ran `validate_job_config.py full_job.json --sec-per-it 0.923… --rows 230` → **PASS**: 30 min
    headroom, ~87 steps, 3 checkpoints.
  - It then launched `training/full_job.json`, which `launch` had written at 01:27:56, before the
    probe existed.
  - All of this is in the manifest's `stage_history`.
- **The probe's completion took the probe branch of the resume Lambda.** Its settle says
  `status: measured`, and the same branch skips the `ModelTrained` emit. That is inferred from the
  branch rather than read off the bus, which has no archive.

The capacity wait that cost PR-11's run eight hours did not recur: the probe started 1 min 44 s after
it was created.

### Not exercised live

- **The rejection paths** (wrong kind, nonexistent job, no handoff) and **the capacity re-entry of
  `FinetuneLaunchFull`**: the agent followed the protocol, so none fired. They are covered offline
  in `TestProbeFullHandoff`, with negative controls.
- **The remediation path** (`RemediateFinetune` → `FinetuneLaunchFull`): this run's iteration 0 did
  not remediate during finetune.

### How the run ended (after the finetune stages)

- **Eval and gate passed.** EvalGenerate 01:52Z → EvalScore → EvalGate. `gate_passed: true` on
  the 120-item held-out acceptance set: `answer_match` 0.725 (Wilson CI [0.639, 0.797], threshold
  0.6) and `format_validity` 1.0 (CI [0.969, 1.0], threshold 0.95). Safety spot-checks passed.
- **Deploy failed, for a reason unrelated to this change.** Deploy was entered at 02:10Z, and the
  endpoint `llmops-student-run-20261016T012010Z-9de45cbf-i0` (ml.g5.xlarge) reached InService at
  02:14Z. The stage was never closed: the agent reported `stage_complete` and `checkpoint` answered
  as "Unknown tool" (the inline-function registry gap in AGENTS.md). The driver logged two
  `tool=stage_complete` turns and a stream salvage ("Inline function callback has no pending handoff
  in this session"), yet the stage did not settle. At 03:08:57Z the agent called `escalate_human`.
  `EscalateFail` → `MarkRunFailed` → `FAILED` at 03:08:58Z.
- **The state machine did not tear the endpoint down.** Deploy's only Catch goes to
  `EscalateFail`, which bypasses `Teardown`.
- **An agent session did, three minutes later.** CloudTrail shows `DeleteEndpoint` at 03:12:07Z
  from an AgentCore session on `llmops-harness-execution`, most likely the escalation triage that
  `EscalatedToHuman` dispatches; no endpoint config remains. The endpoint billed ~58 min
  (02:14–03:12Z). A `deploy/teardown` that I dispatched through the driver at 03:29:30Z (owner's
  choice of route) found it already gone.

None of this touches the handoff path. The driver change is confined to the `job_launched` branch,
and the deploy stage never calls `job_launched`. Two defects remain open, for separate work: a
serviced `stage_complete` that does not settle the stage, and a Deploy Catch that leaves endpoint
cleanup to whatever agent happens to notice, rather than to `Teardown`.
