# Spec: live record

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. `deploy/evidence/VERIFICATION_memory_orphan_sweep.md`: deploy steps and who ran them; pre-deploy
   dry-run (only the two statements; 9 roles unchanged; prompt differs by exactly the clause);
   `simulate_principal_policy` as the role, 10 cases both directions; `05` read-back; the sweep's
   `memory_sweep` block cross-checked against an independent ListMemories; turn protocol, compared
   with the previous 10 scheduled sweeps.
2. CHANGELOG: replace "Not yet deployed" with the result. PROJECT_STATE: monitor harness version.
3. TEST_RESULTS EN + zh-TW: one row. Redaction-scanner counts if they move.
