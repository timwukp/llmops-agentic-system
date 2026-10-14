# Spec: lakehouse source generator

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. `pipeline/lakehouse/generate.py`, standard library only: `generate(seed, scale)` → rows for
   `bronze.{conversation_events, crm_cdc, erp_cdc, web_events}`, held-out eval prompts and
   `expected_defects`; `write()` → canonical JSONL + `manifest.json` with per-file and dataset sha256.
2. Conversations in zh-TW (Traditional) and EN, five intents, template-built.
3. Planted defects, conversation-level ones in disjoint conversations: PII (name, mobile, email,
   national ID, card), toxic, leaked `<think>`, out-of-order, near-duplicate, eval contamination;
   event-level: duplicate resend, schema drift.
4. Deterministic: one seeded RNG, uuid5 ids, fixed base timestamp.
5. Tests: determinism, schema conformance to the deployed tables, an independent oracle per defect.
6. No AWS writes: loading is PR-07, the pipeline step is PR-09.
