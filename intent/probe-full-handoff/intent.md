# Intent: probe and full job are two launch-and-release handoffs, not one

- **Slug:** probe-full-handoff
- **Author:** Claude (AI agent)
- **Date:** 2026-10-15
- **Accepted-by:** Tim WU
- **Status:** accepted

Accepted on the owner's request of 2026-10-15 (chat): "design and implement an explicit two-step
launch-and-release for probe → full, so each training job the pipeline waits on is handed off via
job_launched and resumed by orchestration/resume_pipeline".

## Problem

The finetune `launch` task told the agent to run a 20-step probe, read its measured s/step and VRAM,
and launch the full job. It also told the agent never to poll a job in-turn, but to call `job_launched`
and end the turn. Both cannot hold for two jobs in one task, and live the agent resolved it the same
way three times on 2026-10-15:
- it called `job_launched` naming the PROBE;
- the pipeline resumed into `FinetuneAnalyze` when the probe finished;
- the FULL job was launched from `analyze`, which has no handoff.

`run-20261015T065307Z-97655783`: analyze finished at once and eval waited for the artifact.
`run-20261015T130703Z-f5d05255`: analyze waited in-session through an ml.g5.2xlarge capacity queue
until FinetuneAnalyze's 3600 s timeout failed the run. `run-20261015T104050Z-c7a2c9d8` stalled on the
same ambiguity from the other side (PR-11's `_deliver_if_already_terminal`). PR-11 raised analyze's
timeout to 86400 s as a stopgap, which lets analyze hold a session for a day instead of fixing the
protocol.

## Outcome wanted

- Every training job the pipeline waits on is handed off with `job_launched` and resumed by
  `resume_pipeline`: the probe by `launch` (and `remediate`), the full job by a new `launch_full` task in
  its own state.
- `FinetuneAnalyze` runs only after the FULL job has finished, launches nothing and waits on nothing.
- The driver enforces the protocol rather than trusting the prompt. A `job_launched` is rejected back
  into the turn if the task has no handoff, the job is the wrong kind (probe vs full), or no such job
  exists.

## Acceptance

- Offline guards and negative controls for each guarantee; doc counts re-derived.
- A real `lakehouse` pipeline run, dispatched through the start-pipeline Lambda, shows three
  separate handoffs in sequence: probe handed off from `FinetuneLaunch`, full job from
  `FinetuneLaunchFull`, then `FinetuneAnalyze` entered after the full job Completed.
