# Spec: identifier grounding, in the data and in the gate

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. **Silver rule `strip_ungrounded_ids`** (`pipeline/lakehouse/transform.py`), after `near_dup`
   (which keys on the same case reference): every CRM case reference in an agent reply whose number
   the customer's question does not contain is removed; one the customer quoted stays.
2. **`exact_dup_after_strip`:** pairs that the removal made identical (the case reference was the only
   difference between template-mates) are kept once and counted. A consequence, not a plant: reported,
   not compared.
3. **Generator contract** (`pipeline/lakehouse/generate.py`): `expected_defects.system_assigned_id`
   lists every conversation (every agent reply carries one). `compare_to_plants` holds the rule to
   exactly those minus the conversations an earlier rule dropped.
4. **Gold DQ:** a `CustomSql` rule counting outputs with a case reference must be 0.
5. **Scorer** `pipeline/eval/identifier_grounding.py` (mirrored to `code/eval/` by `03_storage.py`):
   an identifier is `[A-Z]{1,4}-?[0-9]{4,}` bounded by ASCII letters and digits (not `\b`: CJK is a
   word character); grounded when the question contains it. Reports value, count, `identifier_n`,
   Wilson 95% bounds and the ungrounded items; CLI prints one JSON object.
6. **Eval prompt** (`agents/eval/harness.json`): every text run runs the scorer verbatim, records its
   sha256 as `identifier_scorer_sha256`, reports the metric and its bounds, lists ungrounded items, and
   gates it when `params.gates` names it.
7. **Demo preset gate** (`deploy/console/lambda_function.py`): `identifier_grounding ≥ 0.95`. 0.99 can
   never be a decisive pass on 120 items (120/120's Wilson lower bound is 0.969).
8. **The card reads the run that proves it.** The fix's own run wrote `gate.json` in a fourth shape
   (`gates_applied`), which the provenance reader reported as `unknown` (by design, never as "no
   gate"), so the card had no gate rows. `pipeline/contracts/provenance.py` names that shape too.
9. **Tests and controls.** **Live:** deploy the Glue scripts, the eval mirror and the eval harness;
   re-judge the 2026-10-23 model (`eval_only`) with the new gate; one build-then-train run; recorded in
   `deploy/evidence/VERIFICATION_identifier_grounding.md`.
