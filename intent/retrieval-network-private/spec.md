# Spec: llmops-retrieval stops being public

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** shipped
- **Shipped-in:** #196, merged as `4e8aa1c` on 2026-11-21. Further work opens a new chain.

Signed off on the owner's reply of 2026-11-21 (chat): "Sign off".

## Requirements

1. **The sealed policy is data, not prose.** `deploy/09_retrieval.py` gains:
   - `NETWORK_POLICY = f"{COLLECTION}-net"` (today spelled inline twice: create and teardown);
   - `NETWORK_SOURCE_SERVICES = ["bedrock.amazonaws.com"]`;
   - `network_policy(public)`, a pure function. It returns the network policy document as a list
     with one rule set over `collection/llmops-retrieval`. `public=False` gives
     `AllowFromPublic: false` and `SourceServices: NETWORK_SOURCE_SERVICES`; `public=True` gives
     `AllowFromPublic: true`, the form needed only while the index is built.
2. **One writer for the network policy that creates, updates and reads back.**
   `set_network_policy(aoss, public, dry)` calls `get_security_policy(name, type="network")`.
   - `ResourceNotFoundException` → `create_security_policy`.
   - Present but different from `network_policy(public)` → `update_security_policy` with the
     `policyVersion` it just read.
   - Equal → no write.

   It then reads the policy back and raises `SystemExit` unless it equals the requested document.
   It returns `{"policy", "public", "status": created|updated|unchanged, "previous": <document or
   None>, "read_back": "verified"}`.
   Under `dry` it writes nothing and reports what it would do. Compare parsed documents, not
   strings, because `get_security_policy` returns the policy already parsed.
3. **The full deploy path opens only for the index build and always seals.**
   - `ensure_aoss_policies` keeps the encryption and data-access policies. It no longer writes the
     network policy: today's `ConflictException → "exists"` branch is the reason a live public
     policy can never be healed.
   - `main()` calls `set_network_policy(public=True)` immediately before `ensure_collection`.
     `ensure_collection` and `ensure_index` then run inside `try`, and
     `set_network_policy(public=False)` runs in `finally`. A collection that never reaches ACTIVE,
     or an index PUT that fails, therefore still leaves the policy sealed.
   - Everything after (corpus explode, KB, data source, ingest, SSM) runs sealed. Those calls go to
     S3, SSM and Bedrock, and none of them goes to the collection endpoint from outside AWS.
   - The output records the window: `network_window: {opened, sealed, seconds}`.
4. **`--seal-network`: seal an existing collection and nothing else.**
   - It needs only `--region` (`--account-id` and `--dry-run` optional) and is mutually exclusive
     with `--teardown`. `--source-uri`, the eval keys and `--ingest` are neither required nor read.
   - It touches only the AOSS network policy (plus `batch_get_collection` to find the endpoint): no
     IAM, S3, Bedrock, SSM, encryption or data-access call.
   - Real run, in order:
     1. Probe the endpoint, so the output records whether it was reachable before.
     2. Call `set_network_policy(public=False)`.
     3. Poll the probe until it is refused. A policy that reads back sealed while the endpoint still
        answers a signed request after ~2 minutes is `SystemExit`, not success.
   - The probe is a SigV4 `GET {endpoint}/{INDEX}/_count` through the existing `_signed_request`.
     "Refused" means any non-2xx; the code is recorded.
   - If the collection does not exist, the run says so and skips the probe.
   - Dry-run makes no write and no probe, and reports the intended change.
5. **Teardown is unchanged in behaviour.** It deletes `NETWORK_POLICY` by the constant.
6. **Docs.**
   - Module docstring and the `deploy/README.md` 09 row: the network is sealed to Bedrock service
     access, a fresh deploy opens it only for the index build, and `--seal-network` exists.
   - `AGENTS.md`: one gotcha. A public AOSS network policy is revoked by SpringClean (a dummy VPC
     endpoint): the collection is not deleted, it becomes unreachable and keeps billing. The
     deployer's index PUT is the only caller that needs the window.
   - `PROJECT_STATE.md`: replace "Not deployed: retrieval Knowledge Base … nothing is standing" with
     what is live (collection ACTIVE since 2026-08-13, standby replicas on, KB `llmops-org-facts`,
     one ingestion of 300 docs, last search 2026-08-18, network public until the live chain seals
     it). Add the standing-cost line to the posture section.
   - `CHANGELOG.md`; `docs/TEST_RESULTS.md` + `.zh-TW.md` test and negative-control counts; the
     redaction-scanner counts if they move.

## Tests (offline; each written first and seen failing on today's code)

All in the existing `deploy/09_retrieval.py` section of `tests/test_orchestration.py`, reusing the
`retrieval_mod` fixture and `_FakeKbS3`. One new `_FakeAoss` holds policies with versions and
records every call. A recording `_signed_request` fake returns codes from a script and notes the
network policy at the moment of each PUT.

| # | Test | Fails today because |
|---|---|---|
| T1 | `test_the_sealed_network_policy_is_not_public_and_admits_only_bedrock` | `network_policy` does not exist; the only document is public |
| T2 | `test_a_fresh_deploy_builds_the_index_open_and_ends_sealed` (drives `main()` with fakes) | the policy ends public; nothing seals |
| T3 | `test_a_failed_index_build_still_seals_the_network` | no `finally`; the policy ends public |
| T4 | `test_a_redeploy_over_a_public_policy_heals_it` | `ConflictException → "exists"`, no update |
| T5 | `test_seal_network_needs_no_corpus_arguments_and_touches_only_the_network_policy` | argparse rejects `--seal-network` |
| T6 | `test_a_seal_that_does_not_read_back_is_a_failure` | no read-back exists |
| T7 | `test_a_sealed_policy_with_the_endpoint_still_answering_is_a_failure` | no probe exists |
| T8 | `test_seal_network_dry_run_writes_nothing` | the mode does not exist |

T2 asserts the order, not just the end state: the index PUT observed `AllowFromPublic: true`, and
every call after the PUT observed the sealed document.

## Negative controls (`tests/negative_controls/monitor_dispatch.py`)

| Mutation | Must break |
|---|---|
| sealed document's `AllowFromPublic: False` → `True` | T1, T2, T5 |
| the `finally` seal moved to after `ensure_index` (no `finally`) | T3 |
| `set_network_policy` returns early when the policy exists | T4, T5 |
| the read-back comparison removed | T6 |
| the post-seal probe loop treats any response as refused | T7 |
| `--seal-network` writes before checking `dry` | T8 |

## Design decisions

- **A build window, not a VPC endpoint.** The owner accepted the window (intent, question 2). An
  interface endpoint bills per AZ and needs the `02_network.py` VPC that is not built.
- **Always open for a full deploy, even when the index exists.** Whether the index exists can only
  be observed through the data plane the seal closes. Inferring it from the KB's existence is
  unmeasured. A full deploy is a rare, human-run action, and its output now prints the window.
- **The seal lives in code that merges, not in a one-off CLI call.** A hand-flipped policy would be
  undone by the next deploy, which writes `AllowFromPublic: true` today.
- **Remediation, not exemption** (intent constraint).

## Residual risks (stated, not solved here)

- A *fresh* full deploy under the new flow is exercised only offline. The live chain seals the
  existing collection, and no new collection is created live in this change.
- `bedrock.amazonaws.com` as the `SourceServices` value comes from AWS's documented
  Bedrock-to-AOSS private access. The live chain proves it when an agent's retrieve still returns
  hits after the seal. If it does not, the rollback is one `update-security-policy` back to the
  `previous` document, which `set_network_policy` returns and the run prints. That would be an
  incident with its own follow-up PR, not a quiet revert.
- The live seal waits for the PR to merge (intent, question 3). If SpringClean enforces first, the
  collection is unreachable, not deleted, and `--seal-network` replaces its dummy policy.
