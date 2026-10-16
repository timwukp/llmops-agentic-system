# Spec: console intake for the lakehouse modes

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. `TASK_TYPE_REGISTRY` gains two entries.
   - `lakehouse-ingest` (`pipeline_mode: lakehouse_ingest`) asks for:
     - `generator_seed` (number, required);
     - `generator_scale` (string, required);
     - `budget_usd`.
   - `finetune-lakehouse` (`pipeline_mode: lakehouse`) asks for:
     - `lakehouse_dataset` (string, required);
     - `student_model_id` (string, required);
     - `budget_usd`, `training_instance`, `max_iterations`, `keep_endpoint`.
2. **Intake.** `INTAKE_ALIASES` gains the three params, and `generator_seed` is numeric.
   `_verify_lakehouse_dataset` refuses a malformed id outright, then reads
   `lakehouse/reports/<ds>/gold.json` and `lakehouse/landing/<ds>/manifest.json`:
   - `verified`: Gold rows and acceptance-set size, for generator v2;
   - `failed`: no Gold report, or a generator-v1 dataset (no held-out acceptance set: its gate would
     fall back to a val split drawn from the training phrasings);
   - `unverified`: on a read error.
3. **Orchestrator prompt.** Two menu items cover the modes, their required plan keys and the gate's
   decidability at the acceptance set's n (Wilson lower bound at zero failures).
4. **Tests.** The existing dispatcher-derived guards are widened to the new entries. New tests cover
   the verifier and check that the prompt names every required key.
