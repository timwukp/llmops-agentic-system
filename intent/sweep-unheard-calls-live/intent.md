# Intent: record the live deploy and first verified sweep of the unheard-calls fix

- **Slug:** sweep-unheard-calls-live
- **Author:** Claude (AI agent)
- **Date:** 2026-10-15
- **Accepted-by:** Tim WU
- **Status:** accepted

Accepted on the owner's request of 2026-10-15 (chat): "deploy it and run one sweep through the pipeline".

## Problem

#167 merged undeployed, and its evidence, CHANGELOG and TEST_RESULTS say the live sweep is pending. It
is now deployed and has run once through `llmops-monitor-sweep`; the same morning's pre-deploy
scheduled sweep was a fifth failure. Nothing in the repo records either.

## Acceptance

- Evidence §6: deploy read-back, the fifth failure's runtime log, the verification sweep's rows, and
  an explicit statement of which fix paths the live run exercised and which it did not.
- CHANGELOG, PROJECT_STATE, TEST_RESULTS EN + zh-TW updated; redaction counts if they move.
