# Plan: end-to-end retrieve and ingest under the seal

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** 3a839771021eb7d6d333907fc7a3120457eb1522
- **Status:** accepted

Accepted with the intent, on the owner's request of 2026-11-23 (chat): "pls run an end to end test".
`Accepted-for` is `main` at acceptance (#197's merge).

## Files changed

1. `deploy/evidence/VERIFICATION_retrieval_network_private.md`: a new §6; §5 updated.
2. `CHANGELOG.md`, `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md`, `PROJECT_STATE.md` (only if
   a fact changes).
3. `tests/redaction_scan.py`, `tests/test_redaction_scan.py`: census counts, if they move.
4. `intent/retrieval-network-private-live/{intent,spec,plan}.md` (`shipped`),
   `intent/retrieval-network-e2e/{intent,spec,plan}.md`, `.sdlc/active`.

## Order

1. Read-only pre-check of the ingest: corpus keys and bytes, KB-role policy, data-access principal.
   Done 2026-11-23: no change on any.
2. The harness turn.
3. `09_retrieval.py --dry-run`, then `--ingest`.
4. The independent policy read and outside probe.
5. Evidence and docs, the redaction hook and the full suite, then a PR via the Git Data API. The
   owner merges.

## Risks

- **The ingest re-opens the collection for its window.** The index already exists, so the window
  is the seconds between the open and the `finally` seal. The owner accepted this window
  (`retrieval-network-private` intent, question 2).
- **A failed ingest still seals.** The seal runs in the `finally` before the KB steps. A failed
  reconcile is a refusal, recorded as such.
- **The harness turn is one consult-sized turn of tokens.** It touches nothing but the shell commands
  it is asked to run. Its memory records go under a probe `actorId`, which no real agent's namespace
  reads; they are not deleted here.
