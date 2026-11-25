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

  2026-11-23 end to end (§6):
  - a harness turn as `llmops-harness-execution` retrieved 5 matching results with no error;
  - an `--ingest` ran under the seal to COMPLETE (300/300, 0 failed), and its 5.2 s window
    re-sealed and read back.

  Still not exercised (§5): Bedrock *writing* to the sealed collection (that ingest changed
  nothing, so it wrote nothing), and the console's intake verifier.

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

- ~~**A live retrieve issued by a harness** as `llmops-harness-execution`.~~ Run end to end on
  2026-11-23: see §6.1.
- **The console's intake verifier** (`_verify_kb`) was not run. It reads the KB through the
  `bedrock-agent` control plane, which no network policy governs, so this change cannot affect it.
- The console Retrieve and the 04:45–04:50Z search are consistent in time, but nothing here proves
  they are the same event. The network claim does not depend on it: after the seal, any search
  that reaches the collection came through Bedrock.
- **Bedrock writing to the sealed collection is still not exercised.** The 2026-11-23
  `09_retrieval.py --ingest` (§6.2) ran an ingestion job under the seal to COMPLETE, but all 300
  documents were unchanged (0 new, 0 modified). The collection's own metrics show no ingestion
  request after the seal, so Bedrock wrote nothing to it. The first ingest that changes a document
  is the first live test of Bedrock's write path under `SourceServices`. A deliberate test of it
  was deferred by owner decision on 2026-11-24: it would mean changing the content of the index
  the student retrieves from.
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
- A *fresh* full deploy, one that creates the collection and the index, is exercised offline only
  (T2/T3 in `tests/test_orchestration.py`). The window itself (open → index step → seal in the
  `finally`) has now run live once, over an existing collection (§6.2).
- ~~Whether to keep the collection at all.~~ **Kept by owner decision on 2026-11-24.** Its standing
  cost (~$11.52/day or more, since 2026-08-13) is therefore deliberate; `09_retrieval.py --teardown`
  stops it if that changes.

## 6. End to end, 2026-11-23 (`retrieval-network-e2e`)

Requested by the owner ("pls run an end to end test") for the two §5 gaps. Run from `main` at
`3a83977`.

### 6.1 A harness retrieves, as `llmops-harness-execution`

The harness was called through `InvokeHarness`, the same data-plane call the console makes, not
through the console UI, whose Sign in the agent's browser cannot show. The call was one turn of
`llmops_orchestrator`, isolated as `tools/probe_orchestrator_intake.py` isolates its probes: a fresh
session, `actorId` `probe-retrieval-e2e-<hex>` (the shared memory is partitioned by actorId), and no
inline function serviced. The message asked for two shell commands and their raw output, with no
plan and no inline call. The query was the user sentence of corpus ticket `row-00002`, without its
personal details: *"New phone, MFA app has no accounts in it. Locked out of everything."*

| | |
|---|---|
| Turn | started 07:06:28Z, 42.6 s wall, 2 rounds, tools `shell` × 2, `stopReason: end_turn`, no stream error, no inline function called |
| `aws sts get-caller-identity` | `arn:aws:sts::<ACCOUNT_ID>:assumed-role/llmops-harness-execution/BedrockAgentCore-<session>` |
| `aws bedrock-agent-runtime retrieve` (`ULI5VFC3OR`, k = 5) | **5 results, no error**: `row-00288` 1.8283, `row-00080` 1.8273, `row-00132` 1.8273, `row-00184` 1.8200, `row-00236` 1.8195 (all `customer-data/kb-corpus/`) |
| Collection metrics | 1 search in 07:05–07:10Z |

**The self-retrieval criterion, as written, was not met:** `row-00002` itself is not in the five.
The criterion was ill-posed for this corpus. **12 of its 300 rows contain the query sentence
verbatim** (every 26th row: 2, 28, 54 … 288), and all five results are among those 12, all
`category: mfa`, with scores within 0.009 of each other. Five slots, twelve equal matches. The
property the criterion was meant to show, that results are the query's own ticket and not noise,
holds 5/5.

### 6.2 An ingest under the seal

The run was pre-checked read-only to change no content:
- the source's 300 rows render to the same 300 keys with identical bytes;
- the `llmops-kb-service` inline policy and trust are identical to what the script writes;
- the caller is still in `llmops-retrieval-data`.

`--dry-run` came first, then:

`python deploy/09_retrieval.py --region us-east-1 --source-uri s3://<bucket>/customer-data/helpdesk-demo/helpdesk_tickets.jsonl --customer-eval-key customer-data/helpdesk-demo/helpdesk_id_acceptance.jsonl --ood-eval-key customer-data/helpdesk-demo/helpdesk_acceptance.jsonl --ingest`, 07:08:36Z → 07:12:55Z, exit 0:

| Step | Result |
|---|---|
| `network_open` | `updated`, `previous` = the sealed document, `read_back: verified` |
| `collection` / `index` | `exists` / `exists` (the index PUT answered `resource_already_exists_exception`) |
| `network` (the `finally` seal) | `updated`, `previous` = the public document, `read_back: verified` |
| `network_window_s` | **5.2** |
| `corpus` | 300 rows, read back, `stale_removed: 0` |
| KB / data source | `exists` (`ULI5VFC3OR` / `DEGCHBZXWT`) |
| `ingestion` | **COMPLETE, scanned 300, failed 0, reconciled against 300**; indexed new 0, modified 0 |

Afterwards, independently:
- `get-security-policy` reads `AllowFromPublic: false`, `SourceServices: ["bedrock.amazonaws.com"]`,
  version `…_4` (`_3` was the window);
- an outside probe at 07:13:29Z got **401**.

**What the collection's metrics show the ingest touched:**
- 07:05–07:10Z: exactly 1 `IngestionRequestRate` and 1 `IngestionRequestErrors`. That is the
  index PUT during the window, which errored because the index exists.
- After that: **no ingestion request at all**.

So Bedrock's ingestion job ran under the seal and completed, but it wrote nothing to the
collection, because nothing had changed. §5 keeps the write path as not exercised.

### 6.3 Console intake verifier

Not run. Its Sign in uses `window.prompt()`, which the agent's in-app browser cannot show, and no
owner session cookie was imported. It reads the KB through the `bedrock-agent` control plane, which
no network policy governs.
