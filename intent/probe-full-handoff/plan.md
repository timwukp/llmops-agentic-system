# Plan: probe and full job are two launch-and-release handoffs

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** 94c5e5ad5cb381869617b294ef57c90e34cb3ac2
- **Status:** accepted

`Accepted-for` is the merge base with `main` (PR #174's merge). It was first written against #172's
(`80a13c9`) and re-bound when #173 (PR-11) and #174 landed. #173 brought the FinetuneAnalyze 3600 → 86400
stopgap, which this change reverts per requirement 3, plus driver, IAM, negative-control and count
changes that were reconciled. The counts were re-derived on top of both.

## Files changed

1. `pipeline/contracts/tasks.py`: `JOB_HANDOFFS`, `job_kind`; the `prepare` reason's state chain.
2. `agents/finetune/harness.json`: `launch`, `launch_full`, `remediate`, `analyze` bullets.
3. `orchestration/state_machine.asl.json`: `FinetuneLaunchFull`; `FinetuneLaunch`/`RemediateFinetune`
   rewired; `$.probe`.
4. `orchestration/harness_driver/handler.py`: the handoff check in the `job_launched` branch;
   `handle_job_launched` records `job_kind`; a `sagemaker` client.
   `orchestration/resume_pipeline/handler.py`: no `ModelTrained` for a probe.
   `deploy/iam/lambda_roles.json`: `LateJobDelivery`'s comment names the handoff check as its second reader. After
   the rebase onto #173 that statement already grants the read, so no new statement is added.
5. `deploy/console/lambda_function.py`: state map and `STAGE_TASKS`.
6. Tests:
   - `tests/test_orchestration.py`;
   - `tests/test_console_tasks.py`;
   - `tests/negative_controls/monitor_dispatch.py`;
   - `tests/redaction_scan.py` and `tests/test_redaction_scan.py` (tracked-file count).
7. Docs and records:
   - `docs/ARCHITECTURE.md`, `docs/ARCHITECTURE.zh-TW.md`;
   - `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md`;
   - `CHANGELOG.md`, `AGENTS.md`, `PROJECT_STATE.md`;
   - `deploy/evidence/VERIFICATION_probe_full_handoff.md` (new);
   - `intent/probe-full-handoff/{intent,spec,plan}.md`, `.sdlc/active`.
