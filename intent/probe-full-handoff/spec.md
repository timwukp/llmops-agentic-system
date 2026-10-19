# Spec: probe and full job are two launch-and-release handoffs

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. **Contract.** `pipeline/contracts/tasks.py` declares `JOB_HANDOFFS`: (stage, task) → the kind of
   training job that task's `job_launched` must name. The kinds are:
   - `probe` for finetune `launch` and `remediate`;
   - `full` for finetune `launch_full`;
   - `any` for eval `evaluate`.

   `job_kind(hyperparameters)` reads the kind off the job itself: `max_steps` > 0 is a probe, anything
   else is full. Offline, the key set must equal the prompt bullets that promise `job_launched`, in both
   directions.
2. **Prompt** (`agents/finetune/harness.json`):
   - `launch` writes the full payload, launches only the probe, and hands off on it. It must not read the
     probe's metrics, preflight, or launch the full job.
   - A new `launch_full` bullet covers the fresh session after the probe: read
     `sec_per_step.p50_steady` and `peak_vram.training.reserved_fraction_of_device` from the probe's
     `metrics.json`, escalate above 0.95, preflight with `--sec-per-it`/`--rows`, launch the full job,
     and hand off on it.
   - `remediate` hands off on a fresh probe of the revised payload.
   - `analyze` launches nothing; it escalates if the job it is handed is a probe.
3. **State machine:**
   - `FinetuneLaunch` → `FinetuneLaunchFull` → `FinetuneAnalyze`, and `RemediateFinetune` →
     `FinetuneLaunchFull`.
   - `FinetuneLaunchFull` has a CapacityStopped self-catch, TrainingJobFailed → `RemediationChoice`,
     States.ALL last, and a TimeoutSeconds of 86400.
   - `FinetuneAnalyze` keeps a short ceiling (≤ 7200 s), and its only entrants hand off a `full` job.
   - The probe's result goes to `$.probe`, so `$.training` describes the full job.
4. **Driver.** On `job_launched`, before parking:
   - Reject into the turn when the (stage, task) is not in `JOB_HANDOFFS`.
   - Describe the job when a SageMaker client is present:
     - not found → reject;
     - wrong kind → reject;
     - any other read failure → accept, with the check recorded as unverified.
   - Record `job_kind` on the run row. The driver role must allow `sagemaker:DescribeTrainingJob` on
     `training-job/llmops-*`. Since #173 its `LateJobDelivery` statement does, so that statement's comment
     names this second reader and no new statement is added.
5. **Resume.** A probe's completion does not announce `ModelTrained`; the settle output carries
   `job_kind`.
6. **Console.** `FinetuneLaunchFull` maps to the finetune-launch card, and `("finetune", "launch_full")`
   is a session the batch evaluator reconstructs.
7. **Tests.** Extend `tests/test_orchestration.py` for each of the above:
   - the launch-state catches, derived from the prompt;
   - timeout classification;
   - entrant structure;
   - driver dispatch: reject or accept for each kind, not-found, unverified, no handoff.

   Add negative controls in `tests/negative_controls/monitor_dispatch.py`. Re-derive the counts in
   `docs/TEST_RESULTS{,.zh-TW}.md`, update the ARCHITECTURE spine in both languages, and add a
   CHANGELOG entry.
8. **Live.**
   - Deploy the state machine, driver, resume and the finetune harness on top of what is live, which is
     PR-11's tree; deploying `main` alone would roll PR-11 back.
   - Run a real `lakehouse` run through the start-pipeline Lambda.
   - Record the three handoffs in `deploy/evidence/VERIFICATION_probe_full_handoff.md`.
