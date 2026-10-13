# Spec: manifest schema truth

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. `run_id` pattern is what `new_run_id()` mints: `^run-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$`.
2. `trigger_source` is a lowercase label pattern, not an enum: callers name themselves.
3. `models` is role → model-id string for exactly the roles `_resolve_models` can write
   (teacher, student, judge, harness); supply-chain keys stay out.
4. `stages` is documented as shared and agent-writable; only `status` is typed there. The
   driver-written blocks `stage_history`, `attestations`, `escalations` are specified with types.
5. `params`, `plan`, `approval` are described.
6. No lakehouse fields yet: a schema that describes a field no writer writes is the defect this fixes.
   They arrive with the PR that writes them.
7. `tests/test_manifest_schema.py`, standard library only: a validator for the subset the schema uses
   plus a guard that it uses nothing else; `new_run_id`; every `trigger_source` literal in the repo;
   `seed_manifest` output; the driver's records after a real `handle_stage_complete` and
   `_record_manifest_escalation`.
