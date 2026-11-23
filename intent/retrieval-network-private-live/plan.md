# Plan: live seal and record

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** 4e8aa1c4a24e1c20929d7cb255969682c40fe190
- **Status:** accepted

Accepted with the intent, on the owner's request of 2026-11-21 (chat): "已合併，開始上線".
`Accepted-for` is `main` at acceptance (#196's merge).

## Files changed

1. `deploy/evidence/VERIFICATION_retrieval_network_private.md`: new.
2. `PROJECT_STATE.md`, `CHANGELOG.md`, `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md`.
3. `tests/redaction_scan.py`, `tests/test_redaction_scan.py`: counts, if they move.
4. `intent/retrieval-network-private/{intent,spec,plan}.md` (`shipped`),
   `intent/retrieval-network-private-live/{intent,spec,plan}.md`, `.sdlc/active`.

## Order

1. Before-state, read-only.
2. `python deploy/09_retrieval.py --region us-east-1 --seal-network --dry-run`, then without
   `--dry-run`, from this worktree at `main` (`4e8aa1c`).
3. Independent policy read.
4. Console: the owner signs in in the browser pane, then one orchestrator turn runs the retrieve,
   and the intake verifier is read.
5. Evidence and docs, the redaction hook, the full suite, then a PR to `main` via the Git Data API.
   The owner merges.

## Departures recorded during the run

- **Step 4 did not run as planned.** The console's Sign in uses `window.prompt()`, which the agent's
  in-app browser cannot show, and the agent does not enter passwords. So no orchestrator turn and
  no intake-verifier check took place. Instead:
  - The owner, with another agent, made one manual RETRIEVE_ONLY Retrieve in the Bedrock console as
    a console user: 5 results, no error.
  - The agent checked the permission half with `simulate_principal_policy` as
    `llmops-harness-execution`: `bedrock:Retrieve` allowed.

  This covers the network path and the permission separately, not a live harness retrieve end to
  end. The evidence file says so in its verdict and in §5. The intent's runtime-principal and
  intake-verifier acceptance points are recorded as **not run**, not as met.

## Rollback

If the orchestrator's retrieve fails after the seal, run `update-security-policy` back to the
printed `previous` document, re-run the retrieve to confirm, and record it as an incident in the
evidence file. No pass is claimed.
