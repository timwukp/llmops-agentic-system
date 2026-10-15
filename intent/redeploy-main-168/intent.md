# Intent: deploy #168 on top of #167, and correct what that showed

- **Slug:** redeploy-main-168
- **Author:** Claude (AI agent)
- **Date:** 2026-10-15
- **Accepted-by:** Tim WU
- **Status:** accepted

Accepted on the owner's request of 2026-10-15 (chat): "deploy #168 too".

## Problem

1. #167's deploy (08:16Z) replaced the driver #168's branch had deployed at 06:00Z with a tree that
   lacks #168's `lakehouse_review` event mapping. PROJECT_STATE then called that the driver's first
   deploy since August, which CloudTrail contradicts.
2. AGENTS.md says pipeline stages show `recovered_ending_in_prose = 0`. The 2026-10-15 lakehouse run
   was serviced through that path at every stage.

## Acceptance

- Driver, start-pipeline, state machine and console live at `main` (`1c3c7c8`, unchanged in code
  since), read back; #168's IAM, lakehouse jobs and prompts checked live.
- Evidence §7 records the order, the impact check and the restore; PROJECT_STATE and AGENTS.md
  corrected.
