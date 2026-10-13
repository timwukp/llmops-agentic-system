# Spec: S4 and S6 evidence

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. S4 `run()`: seeded zh-TW/EN records with planted entities at exact offsets; regex, Bedrock Guardrails
   (both directions enabled explicitly) and Glue Detect-PII scored on span overlap; best layered
   combination judged against the declared criteria; aggregates only in evidence.
2. S6 `run()`: self-contained VPC (supported AZ IDs), interface + S3 gateway endpoints, a VPC-mode probe
   harness, probe classification; retrying deletes for ENI-held resources; `cleanup` action.
3. Harness: failed deleters reported as LEFT RUNNING; CLI `cleanup`.
4. Evidence files, README, CHANGELOG, PROJECT_STATE (spike leftovers).
