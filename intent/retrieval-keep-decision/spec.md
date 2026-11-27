# Spec: record the keep decision

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** shipped
- **Shipped-in:** #199, merged as `c318138` on 2026-11-27. Further work opens a new chain.

Signed off with the intent, on the owner's request of 2026-11-24 (chat): "Yes, record it".

## Requirements

1. `PROJECT_STATE.md`:
   - the retrieval paragraph's "open owner decision" becomes "kept by owner decision, 2026-11-24";
   - the posture line says the one standing exception is kept deliberately.
2. `deploy/evidence/VERIFICATION_retrieval_network_private.md` §5: the last bullet records the
   decision.
3. `deploy/README.md` row 09: the general rule (tear down after the eval) stays, with this
   collection named as kept by decision.
4. `CHANGELOG.md`: one new bullet in the retrieval section.
5. `intent/retrieval-network-e2e/*` → `shipped`; `.sdlc/active`; the redaction census if it moves.
