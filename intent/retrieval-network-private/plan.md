# Plan: llmops-retrieval stops being public

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** 0459fbc6c169fc0e392e8e75516de8e6b1c722ce
- **Status:** accepted

Accepted on the owner's reply of 2026-11-21 (chat): "Accept plan".

`Accepted-for` is the merge base with `main` (PR #195's merge). The worktree branch
`worktree-retrieval-network-private` is cut from it.

## Files changed

1. `deploy/09_retrieval.py`: `NETWORK_POLICY`, `NETWORK_SOURCE_SERVICES`, `network_policy()`,
   `set_network_policy()`, `probe_endpoint()`, network removed from `ensure_aoss_policies`, the
   open/`try`/`finally`-seal window in `main()`, `--seal-network`, docstring. `main()` keeps its
   signature: the tests set `sys.argv` instead, so the same tests also run red against the old
   `main()` (an `argv` parameter would have made them fail on a `TypeError`, the wrong reason).
2. `tests/test_orchestration.py`, the existing `deploy/09_retrieval.py` section: `_FakeAoss`, fakes
   for the other clients `main()` builds, a recording `_signed_request`, and T1–T8.
3. `tests/negative_controls/monitor_dispatch.py`: six cases, after `m291`'s block (spec table).
4. `deploy/README.md`: the 09 row.
5. `AGENTS.md`: one gotcha line.
6. `PROJECT_STATE.md`: the retrieval KB is deployed (what is live, read 2026-11-21); posture section.
7. `CHANGELOG.md`, `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md`: entry and counts.
8. `tests/redaction_scan.py`, `tests/test_redaction_scan.py`: counts, only if they move.
9. `intent/retrieval-network-private/{intent,spec,plan}.md`, `.sdlc/active`.

## Order of work

1. **Failing tests first.** Write the fakes and T1–T8, then run
   `.venv/bin/python -m pytest tests/test_orchestration.py -q -k "network or seal"`.
   - All 8 must fail, each for the reason the spec's table gives. Record the failure lines for the
     PR description.
   - A test failing for a different reason, such as a fake crashing, gets fixed before any
     implementation.
   - Commit: `test(retrieval): the network must end sealed (failing)`.
2. **Implement** `deploy/09_retrieval.py` per the spec, then re-run the same command until green,
   without editing the tests. If the implementation departs from this plan, this file changes in
   the same commit. Commit: `fix(retrieval): seal llmops-retrieval to Bedrock service access`.
3. **Full suite:** `.venv/bin/python -m pytest tests/ -q --ignore=tests/golden`, green.
4. **Negative controls.** Add the six cases, then run
   `.venv/bin/python tests/negative_controls/monitor_dispatch.py` in the background (~3+ min). Every
   case must be caught, the existing ones included.
5. **Docs and counts:** items 4–8.
   - `tests/test_docs_claims.py` derives every count (suite total, mutations, pairs) and is the
     check that the numbers are right. Both languages get the same numbers.
   - Run `hooks/pre-commit`, the redaction scanner, on the staged tree.
6. **PR** to `timwukp/llmops-agentic-system`, based on `main`, not stacked.
   - If `git commit`/`git push` are hook-blocked here, push through the Git Data API: build the
     blobs, tree and commits from the staged index, then check tree parity.
   - The PR description carries the step-1 failure lines and the control results.
   - The owner reviews and merges. The agent does not merge.

After merge, a **separate chain** `retrieval-network-private-live` (its own intent) runs the seal
from `main` and verifies it:
- dry-run, then the real `--seal-network`;
- policy read-back and the outside probe refused;
- a console consult turn, driven with Playwright after the owner signs in, in which the orchestrator
  runs `aws bedrock-agent-runtime retrieve` against `llmops-org-facts`;
- the console's intake verifier still showing the KB verified.

It writes the evidence file and moves `PROJECT_STATE.md` from "public" to "sealed".

## Tests that prove it

- T1–T8 (spec table), seen failing at step 1 and passing at step 2.
- `_FakeAoss` validates every call's kwargs against botocore's offline `opensearchserverless`
  model (`botocore.validate.validate_parameters` on the operation's input shape). A wrong
  parameter name, such as `version` for `policyVersion`, fails offline instead of at the live seal.
  A fake more forgiving than the API hides exactly the bugs the API will have (AGENTS.md,
  `FakeTable`).
- The six negative controls, each required to break its named tests.
- The existing 09 tests, unchanged and green: eval-key refusal, corpus explode, ingest
  reconciliation, teardown landmarks, standing cost.

## Departures recorded during the build

- `main(argv=None)` was not added (item 1 above).
- The model-validated fake caught a defect in the fake itself: its first draft handed out
  `policyVersion` values like `v1`, and the API's minimum length is 20. Fixed in the fake with the
  API's shape (base64 of `<epoch ms>_<n>`), with no assertion changed. The old code never calls
  update, so this did not affect the red run, and the final test file was re-run red against the
  original script to confirm it.

## Risks

- **The `main()` refactor.** Today it reads `sys.argv` and builds clients inline. Adding `argv` and
  keeping client construction in `boto3.client` (patched in tests) leaves the CLI unchanged. The
  existing tests do not call `main()`, so T2–T5 and T8 are its first coverage.
- **Polling loops in tests.** `ensure_collection`, `ensure_index` and the probe loop sleep, so
  `time.sleep` is patched to a no-op in every test that drives them.
- **The control runner rewrites tracked files.** It runs only in this worktree, never against the
  main checkout, where the owner's staged spike-5 work lives; `git status` is checked before and
  after.
- **Counts conflict with any PR that merges first.** Re-derive them with `test_docs_claims.py`
  after merging `main`, rather than hand-editing.

## Rejected approaches

- **botocore `Stubber`.** It validates parameters too, but it requires an exact call order, and T2
  asserts order through the fake's own observation log. A strict stub queue would make every
  incidental read a test failure.
- **A VPC endpoint so the collection is never public.** The owner accepted the build window
  (intent, question 2). An endpoint bills per AZ and needs the VPC `02_network.py` has not built.
- **Skip the window when the index already exists.** The index is observable only through the
  data plane the seal closes, and inferring it from the KB's existence is unmeasured.
- **Flip the live policy by hand now.** That is outside a PR, and the next deploy would undo it
  (`AllowFromPublic: true` today).
- **A SpringClean exemption.** That is the opposite of the intent's remediation constraint.
