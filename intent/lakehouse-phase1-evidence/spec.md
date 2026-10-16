# Spec: held-out acceptance set and Phase 1 live evidence

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. **Generator v2** (`GENERATOR_VERSION = "2"`): `_FAMILIES` replaces `_TEMPLATES`. Each (intent,
   language) has one answer, ≥4 train phrasings and ≥2 held-out phrasings, with the two sets
   disjoint. The acceptance set is `eval_prompts.jsonl`, rows
   `{prompt_id, lang, intent, prompt, reference}`:
   - 40 items at small scale, 120 at demo;
   - an equal share per intent, every prompt distinct;
   - no case number in a reference.

   Planted contamination copies acceptance prompts, as before.
2. **Export:** `--acceptance_uri` (landing `eval_prompts.jsonl`, in `run_arguments` and in the ASL).
   `acceptance_jsonl` canonicalises the set and refuses rows without references, duplicates or an
   empty file; a refused set is reported absent with its reason. The output is `acceptance.jsonl` in
   the extract bundle, recorded under `files.acceptance` with its sha256.
3. **Driver:** `ACCEPTANCE_CLAIM_KEYS` and an attestation of kind `acceptance_set`, measured exactly
   like `gold_extract`.
4. **Prompts:**
   - The review hashes the acceptance set, checks it against training user turns (any match
     escalates), records it in `stats.json` and claims it.
   - Eval's lakehouse fallback is `acceptance_uri`, then `val_uri`.
5. **Live:** deploy the change, then run the smoke, a `lakehouse_ingest` run, and a `lakehouse` run
   with the owner's gates `{format_validity: 0.95, answer_match: 0.6}`. Record the verdict in
   `deploy/evidence/VERIFICATION_lakehouse_phase1.md`.
