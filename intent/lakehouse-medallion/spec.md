# Spec: lakehouse medallion

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. `pipeline/lakehouse/transform.py` (stdlib): `bronze_to_silver` with named rules dedup_event_id,
   tolerate_drift, order_by_turn, strip_think, drop_toxic, redact_pii (checksum regex + injected name
   detector), decontaminate, near_dup (case reference + sequence similarity); `silver_to_gold`
   (deterministic pair_id split); `to_messages`; `compare_to_plants`; SILVER_DQDL / GOLD_DQDL.
2. Glue 5 jobs `land_bronze`, `bronze_to_silver` (names via a Bedrock guardrail, version-pinned),
   `silver_to_gold`; INSERT OVERWRITE semantics; DQ evaluated in-job; reports to S3.
3. `deploy/10_lakehouse.py`: guardrail `llmops-lakehouse-pii` (NAME, both directions, numbered version),
   the three jobs, scoped Glue-role grants (landing read, reports write, the one guardrail, DQ publish),
   `--verify` covering them in both directions, `--smoke` against the generator's plants.
