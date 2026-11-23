# Intent: close the two gaps the live seal left, end to end

- **Slug:** retrieval-network-e2e
- **Author:** Claude (AI agent)
- **Date:** 2026-11-23
- **Accepted-by:** Tim WU
- **Status:** accepted

Accepted on the owner's request of 2026-11-23 (chat): "pls run an end to end test". The request
quoted the two gaps that `deploy/evidence/VERIFICATION_retrieval_network_private.md` §5 lists as
not run.

## Problem

`llmops-retrieval` was sealed to Bedrock service access on 2026-11-21 (#196, #197). Two things were
proven only in halves or not at all:

1. **No retrieve issued by a harness, end to end, as `llmops-harness-execution`.** The network path
   was shown by a Bedrock-console Retrieve made as a console user, and the permission by
   `simulate_principal_policy`.
2. **No ingestion under the seal.** Also, the deploy-time window (open → index → seal in a
   `finally`) has run only offline.

## Acceptance

- **A harness turn retrieves.** One turn of `llmops_orchestrator` through `InvokeHarness`, the same
  data-plane call the console makes, with a fresh session and a unique `actorId` so nothing enters
  the shared memory's real namespaces. The agent runs `aws sts get-caller-identity` and
  `aws bedrock-agent-runtime retrieve` against `ULI5VFC3OR` with a ticket known to be in the corpus.
  Pass: the caller is `llmops-harness-execution`, results come back with no error, and the ticket's
  own document is among them.
- **An ingest runs under the seal.** `deploy/09_retrieval.py --ingest` from `main`, `--dry-run`
  first. It is pre-checked to change no content: same 300 doc keys and bytes, KB-role policy
  unchanged, the caller still in the data-access policy. Pass: the window opens and seals with the
  read-back verified; ingestion COMPLETE with `scanned == 300` and `failed == 0`; after the run the
  policy reads back sealed and the outside probe is refused.
- **The console intake verifier** runs only if the owner can sign in (its Sign in uses
  `window.prompt()`, which the agent's browser cannot show). Otherwise it stays recorded as not run.
- Evidence appended to the same file; TEST_RESULTS EN + zh-TW; CHANGELOG; chain
  `retrieval-network-private-live` set to `shipped`.
