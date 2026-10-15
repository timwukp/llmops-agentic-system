# Intent: record the live deploy and first verified memory-orphan sweep

- **Slug:** memory-orphan-sweep-live
- **Author:** Claude (AI agent)
- **Date:** 2026-10-15
- **Accepted-by:** Tim WU
- **Status:** accepted

Accepted on the owner's request of 2026-10-15 (chat): "合併後部署並跑一次 sweep 驗證". The owner ran
both deploy commands in their own terminal after the agent's IaC apply was refused by the session's
permission classifier.

## Problem

#164 shipped the sweep's memory-orphan check undeployed. It is now deployed and has run once through the
pipeline; nothing in the repo records that, and CHANGELOG still says "Not yet deployed".

## Acceptance

- Evidence file with the IAM simulation, the deploy read-back and the sweep's own output, redacted.
- TEST_RESULTS EN + zh-TW row; CHANGELOG and PROJECT_STATE updated.
