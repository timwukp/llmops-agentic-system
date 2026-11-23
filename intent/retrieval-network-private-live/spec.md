# Spec: live seal and record

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** shipped
- **Shipped-in:** #197, merged as `3a83977` on 2026-11-23. Further work opens a new chain.

Signed off with the intent, on the owner's request of 2026-11-21 (chat): "已合併，開始上線".

## Requirements

1. `deploy/evidence/VERIFICATION_retrieval_network_private.md`:
   - the before-state (policy, collection, KB);
   - the `--seal-network --dry-run` output, then the real run's `probe_before`, `network.previous`,
     `network.status`, `read_back` and `probe_after`;
   - an independent `get-security-policy` read;
   - the console orchestrator turn: the command it ran, hit count, and whether the probed corpus
     document came back;
   - the intake verifier's KB line;
   - who ran what.

   Redacted: no account id, account-bearing ARN or bucket name.
2. PROJECT_STATE: the network policy is sealed (date, `previous` recorded for rollback). CHANGELOG:
   replace "Not yet applied live" with the result.
3. TEST_RESULTS EN + zh-TW: one row each, same numbers.
4. `intent/retrieval-network-private/{intent,spec,plan}.md` → `Status: shipped`.
5. Redaction-scanner counts if they move.
