# Plan: a rejected stage_complete is heard, and a failed Deploy is torn down

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** 50b6afea02f3d928a8ae26a494680e12f78b02a2
- **Status:** accepted

`Accepted-for` is the merge base with `main` (PR #175's merge). It was first written against #173's,
then #174's; #174 (the console's lakehouse intake) and #175 (the probe/full handoff) landed during this
change. They overlap it only in the shared counts, which were re-derived on top of them, in the
negative-control numbering (this change's controls moved from m356–m359 to m374–m377), and in the
timeout-policy test's state sets. The probe/full-handoff change is now on `main` (#175) and live; this plan's driver change is in a
different hunk, and one cross-PR test pins the two together (a `check_handoff` refusal on an end_turn
call reaches the agent as text).

## Files changed

1. `orchestration/harness_driver/handler.py`: the end_turn override, `_tool_result_content`,
   and the `stage_complete` rejection branch.
2. `orchestration/state_machine.asl.json`: Deploy's Catch and the new `TeardownAfterDeployFailed`.
3. `tests/test_orchestration.py`: the live-shape replay, the text-ack test, and the two ASL path
   guards. `tests/negative_controls/monitor_dispatch.py`: m374–m377.
4. `AGENTS.md`, `CHANGELOG.md`, `PROJECT_STATE.md`, `docs/ARCHITECTURE.md`,
   `docs/ARCHITECTURE.zh-TW.md`, `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md`, and
   `deploy/evidence/VERIFICATION_deploy_settle_and_teardown.md`.
   `tests/redaction_scan.py` and `tests/test_redaction_scan.py` (tracked-file count).
5. `intent/deploy-settle-and-teardown/{intent,spec,plan}.md` and `.sdlc/active`.

## Not in this change

- Deploying. `07_lambdas.py --only driver --only state_machine` waits for the owner's go-ahead.
  It deploys from a fresh `origin/main` after this PR merges, which then contains #175 too, so it
  cannot roll back the probe/full change that is already live.
- Verifying a prefix claim with `ListObjectsV2`. That needs `s3:ListBucket` on the driver role, an
  IAM change for the owner to apply. Until then the agent is told to name objects.
- The escalation triage loop: 34 `EscalationResolved` directives for this run between 03:12 and
  03:30Z. A separate defect, flagged separately.
- The salvage prompt's wording ("call your pending inline function") after a refusal the driver
  caused. With requirement 1 the driver no longer causes one.
