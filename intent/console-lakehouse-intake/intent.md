# Intent: start a lakehouse run from the console

- **Slug:** console-lakehouse-intake
- **Author:** Claude (AI agent)
- **Date:** 2026-10-16
- **Accepted-by:** Tim WU
- **Status:** accepted

## Problem

The two lakehouse pipeline modes (PR-09) and their held-out acceptance set (PR-11) work end to end,
but only when dispatched straight to the start-pipeline Lambda. The console's task catalog offers
neither mode, and the orchestrator's prompt does not know how to plan one. A customer therefore cannot
reach them through the signed-plan path every other run takes. That path was step 2 of PR-11's plan
("a `lakehouse_ingest` run started from a console task"). The owner asked to continue with the next
PR after #173 (2026-10-16).

## Desired outcome

The catalog gains two entries:
- **lakehouse-ingest**: build a governed dataset.
- **finetune-lakehouse**: train on a Gold dataset.

Their forms ask for what the dispatcher demands. Intake verifies a named dataset from its reports,
and refuses one without a held-out acceptance set before a plan is written. The orchestrator knows
both modes, their required plan keys, and that a gate must be decidable at the acceptance set's size.

## Acceptance

- Each form's required fields cover `MODE_REQUIRED_PARAMS`, and `finetune-lakehouse` names the student
  role (`MODE_REQUIRED_ROLES`). Both are derived from the dispatcher, not restated.
- Intake marks a v2 dataset with a Gold report `verified`. It marks a v1 dataset (no acceptance set)
  and an unknown one `failed`, with the reason.
- The orchestrator prompt names both modes and every key their dispatch requires.
