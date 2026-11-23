# VERIFICATION — llmops-retrieval sealed to Bedrock service access (2026-11-21, us-east-1)

- **Change:** #196 (`4e8aa1c`): `deploy/09_retrieval.py` network policy as data, `set_network_policy`
  with read-back, the deploy-time build window, `--seal-network` with an outside probe.
- **Why:** an AWS SpringClean dry-run notice (2026-11-21) listed `llmops-retrieval` among publicly
  shared OpenSearch Serverless collections. When enforced, SpringClean swaps in a dummy VPC endpoint:
  the collection is not deleted, it becomes unreachable to Bedrock too and keeps billing.
- **Verdict:** **PASS for the network seal.**
  - Public access is refused: the same signed outside request got 200 before and 401 after.
  - Bedrock's service access still retrieves: the owner's console Retrieve returned 5 results with
    no errors.
  - The harness role is allowed to Retrieve (IAM simulation).
  - The owner reports that SpringClean's 2026-11-23 report no longer lists `llmops-retrieval`.

  Two checks the intent named did not run (§5): a live retrieve issued by a harness, and the
  console's intake verifier.

## 1. Before (read-only, 2026-11-21 12:12:00Z)

| Resource | State |
|---|---|
| `llmops-retrieval-net` | `AllowFromPublic: true`; policyVersion `MTc4NjYyMzQxNDU3Nl8x` (base64 of `<epoch ms>_1`), unchanged since the collection's creation on 2026-08-13; no description |
| collection `llmops-retrieval` | ACTIVE, standby replicas ENABLED |
| KB `llmops-org-facts` | ACTIVE |

## 2. Seal, run from `main` at `4e8aa1c`

The run used this checkout, reset to `origin/main` after #196's merge. `deploy/09_retrieval.py` is
byte-identical to `main`.

1. `python deploy/09_retrieval.py --region us-east-1 --seal-network --dry-run` →
   `"status": "would set sealed to bedrock.amazonaws.com"`, `"probe": "skipped (dry run)"`. No write,
   no probe.
2. `python deploy/09_retrieval.py --region us-east-1 --seal-network`, 12:12:28Z → 12:12:33Z:

| Field | Value |
|---|---|
| `probe_before` | **200**: a SigV4 `GET /llmops-kb-index/_count` from outside AWS reached the collection |
| `network.status` | `updated` |
| `network.previous` | `[{"Rules": [{"Resource": ["collection/llmops-retrieval"], "ResourceType": "collection"}], "AllowFromPublic": true}]` (the rollback document) |
| `network.read_back` | `verified` |
| `probe_after` | **401**: the same signed request, refused on the first post-seal try |

The probe's host was explicitly allowed through the agent's network sandbox, so the 200 is a real
answer from the collection. A probe stopped by the sandbox's proxy would have come back as
`unreachable: …` and proved nothing; that is why `probe_before` had to be 200.

## 3. Independent read-back

- `aws opensearchserverless get-security-policy --name llmops-retrieval-net --type network` returns
  `AllowFromPublic: false`, `SourceServices: ["bedrock.amazonaws.com"]`, policyVersion
  `MTc5NTI2MzE1MjU4M18y` (`…_2`), and description `sealed to bedrock.amazonaws.com --
  deploy/09_retrieval.py`.
- A second outside probe at 12:12:51Z returned `401 {}` again.

## 4. Bedrock still retrieves after the seal

The intent planned this as an orchestrator turn in the admin console. That did not happen. The
console's Sign in uses `window.prompt()`, which the agent's in-app browser cannot show, so the agent
could not sign in, and it does not enter passwords. Instead, the **owner, with another agent, ran a
manual test in the Bedrock console**, and that is what is recorded here:

| | |
|---|---|
| When | ~2026-11-23 04:48–04:50 UTC, estimated from that session's timeline, not exact to the second |
| Who | a federated console user (the owner), **not** `llmops-harness-execution` |
| Path | Bedrock console → Knowledge Bases → `llmops-org-facts` (`ULI5VFC3OR`) → Test, mode RETRIEVE_ONLY (output was chunks with no generated answer, so the mode took effect). Other settings were left at their defaults and not inspected one by one |
| Query | free text, "What is the cost approval threshold?", not a ticket from the corpus, so no self-retrieval check |
| Result | **5 results, no error** (no AccessDenied, ValidationException, 401 or 403). All five were `category: access` sample tickets. Scores and source URIs were not recorded, and the results' personal names and emails are deliberately not copied here |
| Calls | exactly one Retrieve; no configuration changed; no console task, plan or run |

A console Retrieve and a harness Retrieve are the same `bedrock-agent-runtime` Retrieve call. In
both, Bedrock reaches the collection with the KB's own service role (`llmops-kb-service`), not the
caller's identity, so the post-seal network path is the same either way. What the caller's identity
decides is whether Bedrock accepts the call, which is IAM, and the seal changed no IAM. That half
was checked as the runtime principal with `simulate_principal_policy`
(`PolicySourceArn = llmops-harness-execution`, resource `knowledge-base/ULI5VFC3OR`):

| Action | Decision | Matched |
|---|---|---|
| `bedrock:Retrieve` | **allowed** | inline policy `llmops-permissions` (statement `BedrockKBRetrieveReadOnly` in `deploy/iam/harness_execution_role.json`) |
| `bedrock:RetrieveAndGenerate` | implicitDeny | none, as designed |
| `bedrock:StartIngestionJob` | implicitDeny | none, as designed |

Also observed read-only by the owner in the console at the same time:
- the network policy shows Access type Private, AWS service private access `bedrock.amazonaws.com`;
- the collection page lists no VPC endpoints. SpringClean's dummy endpoint would be an ID inside
  the network policy, not an endpoint on that page, so the proof that none is present is the
  policy itself: the read-back in §3 has no VPC endpoint entry at all;
- the collection is Active, 5.30 MiB, with `llmops-retrieval-data` attached;
- SpringClean's report of 2026-11-23 01:28 UTC no longer lists `llmops-retrieval`.

**Corroboration from the collection's own metrics** (`AWS/AOSS`, 5-minute sums from 2026-11-21
12:00Z):

| Bucket (UTC) | `SearchRequestRate` | `SearchRequestErrors` | What it is |
|---|---|---|---|
| 2026-11-21 12:10–12:15 | 1 | 0 | `probe_before`, the last request from outside AWS before the seal |
| 2026-11-23 04:45–04:50 | 1 | 0 | a search that reached the collection after the seal, which only Bedrock's service access can do now |

The refused probes after the seal do not appear as searches: they were stopped at the network layer.

## 5. What this does not cover

- **A live retrieve issued by a harness** as `llmops-harness-execution`, the intent's planned check.
  Its two halves are covered separately (the network path by the console Retrieve, the permission by
  simulation), but no agent turn ran it end to end.
- **The console's intake verifier** (`_verify_kb`) was not run. It reads the KB through the
  `bedrock-agent` control plane, which no network policy governs, so this change cannot affect it.
- The console Retrieve and the 04:45–04:50Z search are consistent in time, but nothing here proves
  they are the same event. The network claim does not depend on it: after the seal, any search
  that reaches the collection came through Bedrock.
- **Ingestion under the seal is not exercised live.** Retrieve is one Bedrock path into the
  collection and ingestion (`StartIngestionJob`) is another. The same `SourceServices` rule is
  expected to cover both, but the next `09_retrieval.py --ingest` is the first live test of it.
- **"Refused" is read from a 401, which is inferred, not documented.** The collection answered 401
  with an empty body, not the 403 a network denial might be expected to return. It is read as the
  seal for three reasons:
  - the same signer sent the same request and got 200 five seconds earlier;
  - the policy was the only change in between;
  - the 401 persisted 18 seconds later.

  This is a before/after contrast, not a documented error code.
- **SpringClean's report dropping the collection supports "fixed" only if that report still lists
  the other four collections.** The owner reported the absence; whether the others are still listed
  has not been confirmed.
- A *fresh* full deploy under the new flow (open → build index → seal) is exercised offline only
  (T2/T3 in `tests/test_orchestration.py`). This run sealed an existing collection.
- Whether to keep the collection at all (~$11.52/day or more, standing since 2026-08-13) is an open
  owner decision.
