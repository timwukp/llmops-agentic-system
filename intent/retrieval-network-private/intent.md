# Intent: llmops-retrieval stops being public, so SpringClean has nothing to revoke

- **Slug:** retrieval-network-private
- **Author:** Claude (AI agent)
- **Date:** 2026-11-21
- **Accepted-by:** Tim WU
- **Status:** accepted

Raised by the owner on 2026-11-21 (chat), from an AWS SpringClean dry-run notice: "if yes, before
SpringClean delete it, we need protect it". Process asked for: spec-driven, TDD, plan → design →
build → test, every change through a PR to `timwukp/llmops-agentic-system`.

Accepted on the owner's reply of 2026-11-21 (chat): "Accept as proposed" — the three open questions
below are answered as proposed: verify through a console orchestrator turn, accept the re-deploy
window, wait for the PR to merge.

## Problem

1. SpringClean lists five OpenSearch Serverless collections in this account as publicly shared.
   Exactly one belongs to this project: `llmops-retrieval` (us-east-1, created 2026-08-13), the
   vector store under KB `llmops-org-facts`. When SpringClean leaves dry-run it does **not** delete
   the collection. It replaces the network policy with a dummy VPC endpoint, and the collection
   becomes unreachable to everyone, Bedrock included. Every `bedrock-agent-runtime retrieve` the
   pipeline runs (the data-prep curate probe, eval's open-book answers) then fails, while the
   collection keeps billing.
2. The cause is in this repo. `deploy/09_retrieval.py` writes the network policy with
   `AllowFromPublic: true`, and on a re-run a `ConflictException` is reported as `exists` and the
   policy is never compared or updated. Changing the constant alone would not fix the live policy,
   and a later redeploy could not heal it.
3. Only one caller needs the collection's endpoint reachable from outside AWS: the deployer's
   index `PUT` in `ensure_index` (a SigV4 HTTP request from the deployer's machine, because boto3 has
   no AOSS index API). Bedrock ingest and Bedrock retrieve reach the collection as an AWS service.
   The admin console (`llmops-admin-api` → Lambda `llmops-admin`) reads the KB only through the
   `bedrock-agent` control plane (`_verify_kb`), which no network policy governs.
4. `PROJECT_STATE.md` says the retrieval KB is not deployed. Live, read 2026-11-21: the collection is
   ACTIVE with standby replicas ENABLED, was ingested once (2026-08-13, 300 documents), and was last
   searched 2026-08-18. The r6d protocol's "teardown the same day" never happened.

## Desired outcome

- The steady state of `llmops-retrieval-net` is `AllowFromPublic: false` with
  `SourceServices: ["bedrock.amazonaws.com"]`.
- `deploy/09_retrieval.py` produces that state on a fresh deploy and restores it on any re-run. It
  opens the collection only for the index-creation window, seals it afterwards, reads the policy
  back, and exits non-zero if the seal does not verify.
- A narrow `--seal-network` mode applies the seal to an existing collection without the corpus and
  eval arguments a full deploy needs, and without touching anything else.
- The live collection is sealed **from merged `main`**, then verified, in a follow-up chain
  (`retrieval-network-private-live`), the same pattern as `memory-orphan-sweep` /
  `memory-orphan-sweep-live`.

## Affected

- Code: `deploy/09_retrieval.py`; new offline tests.
- Docs: `PROJECT_STATE.md` (correct the "not deployed" claim), `CHANGELOG.md`,
  `docs/TEST_RESULTS.md` + `docs/TEST_RESULTS.zh-TW.md`, `deploy/README.md` (the 09 row),
  `AGENTS.md` (one gotcha line), redaction-scanner counts if they move.
- Live (follow-up chain only): the AOSS security policy `llmops-retrieval-net`. Nothing else: not the
  collection, its data-access policy, the KB, the console, or any harness.
- Out of scope: the other four flagged collections (`demo1234`, `bedrock-knowledge-base-v0ioeb`,
  `-ybyt63` in us-east-1, `-9qoqti` in us-west-2). They are not this project's. Their own KBs date
  from 2024-03 and 2025-08, and none was searched in the ~15 months CloudWatch retains.

## Constraints

- Every change lands through a PR to `timwukp/llmops-agentic-system`, and a human merges. The agent
  does not run the live seal from an unmerged branch.
- Tests are offline by construction: `conftest.py` refuses sockets.
- Public repo: no account ids, account-bearing ARNs or bucket names in any file or diff.
- Remediation, not exemption. The collection stops being public; no SpringClean exemption is sought.
- Whether the collection should exist at all (standing cost ~$11.52/day or more, not yet checked
  against Cost Explorer) is a separate owner decision this intent does not make.

## Success criteria

1. Offline: new tests fail against today's `09_retrieval.py` (public policy document, no update
   path, no seal) and pass after the change, without editing the tests.
2. Live, after merge: `get-security-policy --name llmops-retrieval-net --type network` returns
   `AllowFromPublic: false` and `SourceServices: ["bedrock.amazonaws.com"]`.
3. Live, as the runtime principal: a Bedrock retrieve against `llmops-org-facts` returns hits for a
   ticket known to be in the corpus, issued by an agent running as `llmops-harness-execution`.
4. Live, negative: a signed request from the deployer's machine straight to the collection endpoint
   is refused.
5. Live, console: the admin console, driven with Playwright after the owner signs in, still shows
   the KB as verified (ACTIVE, last ingestion COMPLETE, 300 documents).
6. Evidence file `deploy/evidence/VERIFICATION_retrieval_network_private.md` (redacted);
   `PROJECT_STATE.md` matches what is live.

## Open questions for the owner

1. **Verification channel for criterion 3.** Proposed: one console consult turn in which the
   orchestrator runs `aws bedrock-agent-runtime retrieve` from its shell. All seven harnesses share
   `llmops-harness-execution`, which holds `bedrock:Retrieve`, so this goes through the system as the
   same principal data-prep and eval use, for roughly one turn of tokens. The data-prep curate probe
   is the production path, but it runs only inside a full training run (~$150–200). A direct
   retrieve under deployer credentials costs $0 but proves nothing about the runtime role. Proceed
   with the console turn?
2. **Re-deploy window.** A fresh deploy must open the collection for a few minutes to create the
   index, then seal it. Accept that window, or require a VPC endpoint (billed per AZ, needs the
   `02_network.py` VPC) so the collection is never public?
3. **Timing.** SpringClean is in dry-run and its enforcement date is unknown. If it enforces before
   the PR merges, the collection becomes unreachable but stays intact, and the same seal replaces
   the dummy policy. Is waiting for merge acceptable, given that the collection has had no search
   since 2026-08-18?
