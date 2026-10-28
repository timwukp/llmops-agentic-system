# Spec: gate/1

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. **Schema** (`pipeline/contracts/gate.schema.json`): required `schema_version` ("gate/1"),
   `run_id`, `iteration`, `decided_at`, `report_uri`, `gate_passed`, `gates`; each gate requires
   `threshold`, `value`, `k`, `n`, `ci_low`, `ci_high`, `rule` (wilson95 | interval | band | missing)
   and `verdict` (pass | fail | borderline), with an optional `directive`; optional `checks`
   (`passed`, `detail`). Extra keys allowed.
2. **Rules** (`pipeline/contracts/gate_contract.py`): the dispatched run and iteration; exactly the
   signed gates, at the signed bars; each verdict the one its rule gives (wilson95 recomputes the
   interval from k and n and checks value = k/n; band is the prompt's ±0.05; missing is a fail);
   no borderline without a directive; `gate_passed` true only when every gate passed or was
   resolved and every check passed, false not when everything passed; and equal to the
   stage_complete's `metrics.gate_passed`, a null refused. Hardened after the 2026-10-28
   security audit: NaN/Infinity are refused (Python's json accepts them, and a NaN bar passes
   every comparison); the four proportion metrics must be `wilson95` (or `missing`); a
   borderline's directive must be one delivered to this run, and one counted as passed must
   come from a person, not the conductor agent.
3. **Driver** (`orchestration/harness_driver/handler.py`): on (eval, gate) `stage_complete`, read
   `runs/<run_id>/evaluation/gate.json` beside the manifest (at most 256 KB), the signed gates
   from the run's DynamoDB row, and the delivered directives from the stage-events table. The
   harness role can write the manifest but neither table. A run whose row predates this falls
   back to the manifest and logs it. Any failed lookup is a rejection. Reject with the errors
   (capped) as the sweep's check does. **start-pipeline** puts `signed_gates` on the row. The bundle ships
   the contract and its schema (`deploy/07_lambdas.py`).
4. **Prompt** (`agents/eval/harness.json`, the gate clause): the path, the shape, the rules.
5. **Readers:** `provenance.gate_view` reads `gate/1` first and the legacy shapes after; the
   console's comment updated.
6. **Docs:** `docs/ARCHITECTURE.md` (EN/zh) and `AGENTS.md`.
