# Intent: seal the live llmops-retrieval from merged main, and record it

- **Slug:** retrieval-network-private-live
- **Author:** Claude (AI agent)
- **Date:** 2026-11-21
- **Accepted-by:** Tim WU
- **Status:** accepted

Accepted on the owner's request of 2026-11-21 (chat): "已合併，開始上線", after merging #196
(`4e8aa1c`). This is the follow-up chain that `intent/retrieval-network-private/intent.md` and its
plan already describe, with the three open questions answered there: verify through a console
orchestrator turn, accept the re-deploy window, wait for merge.

## Problem

#196 made `deploy/09_retrieval.py` produce and heal a sealed network policy, but nothing live has
changed: `llmops-retrieval-net` is still `AllowFromPublic: true`, which is what the AWS SpringClean
dry-run flagged. `PROJECT_STATE.md` says "public" and CHANGELOG says "Not yet applied live".

## Acceptance

- `--seal-network` run from `main` (dry-run first): the endpoint answered before, the policy reads
  back sealed, and the endpoint refuses a signed request from outside AWS afterwards.
- As the runtime principal (`llmops-harness-execution`), through the system: an orchestrator turn in
  the admin console, driven with Playwright after the owner signs in, runs
  `aws bedrock-agent-runtime retrieve` against `llmops-org-facts` and gets hits.
- The console's intake verifier still reports the KB verified.
- Evidence file (redacted); PROJECT_STATE moves to "sealed"; CHANGELOG and TEST_RESULTS EN + zh-TW
  updated; chain 1 set to `shipped`.
- If Bedrock cannot retrieve after the seal, restore the `previous` document the run printed and
  record an incident instead of a pass.
