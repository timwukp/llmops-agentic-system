# Spec: record the live deploy of the settle-and-teardown change

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

Signed off under the same 2026-10-21 instruction as the intent.

## Requirements

1. `deploy/evidence/VERIFICATION_deploy_settle_and_teardown.md` gains a "Deploy (2026-10-21)"
   section. It covers:
   * the pre-deploy checks (drift audit, RUNNING executions, dry run);
   * the deploy command and its result;
   * the read-back (live definition, a byte comparison of the live driver bundle, the post-deploy
     drift audit);
   * what is not yet exercised live.
   Its "Not done" item about deploying is updated to match.
2. PROJECT_STATE's harness-versions section says the state machine (37 states) is at `ecc2e50` since
   2026-10-21, and that the driver deployed with it was superseded on 2026-10-23 by #177's `7f3d0ee`,
   which keeps the change. It no longer names the 10-16 deploy as current for either.
3. AGENTS.md's end_turn gotcha says the fix is deployed, and since when.
4. CHANGELOG gets one entry.
5. The census count in `tests/redaction_scan.py` and `tests/test_redaction_scan.py` is re-derived
   for the three new intent files.
