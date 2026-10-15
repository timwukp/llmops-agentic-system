# Spec: live record

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. `deploy/evidence/VERIFICATION_sweep_unheard_calls.md` §6 replaces "Pending": preflight; `07_lambdas.py
   --only driver --only monitor_sweep` and `05_harnesses.py --agent monitor` read-backs; the 08:00Z
   scheduled sweep's failure (every inline function answered `Unknown tool`); the verification sweep's
   protocol, stage_complete and `sweep#sweep` rows; live-vs-offline coverage of each fix path.
2. CHANGELOG: replace "Deploy and a live sweep wait for the owner" with the result. PROJECT_STATE:
   monitor v15; the driver deploy also shipped #154 and #165 driver changes.
3. TEST_RESULTS EN + zh-TW: the row's result. Redaction-scanner counts.
