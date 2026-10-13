# VERIFICATION — manifest schema against the live run manifests (2026-10-13)

**Question.** Does `pipeline/contracts/manifest.schema.json` describe the manifests this platform
actually writes? Phase 1 extends the manifest with lakehouse provenance, so the schema had to be
true before anything was added to it.

**Method.** Read-only. Every `s3://<DATA_BUCKET>/runs/<run_id>/manifest.json` (40 objects) was
validated against the schema at `main` (3a3a61d) and against the corrected schema, using the
validator in `tests/test_manifest_schema.py` (the same code the suite runs).

## Result

| Schema | Manifests valid |
|---|---|
| before (3a3a61d) | **0 / 40** |
| after (this change) | **36 / 40** — the 4 others explained below |

## What the old schema got wrong, measured

| Field | Schema said | Live manifests hold |
|---|---|---|
| `run_id` | `llmops-<ts>-<6 hex>` | `run-<ts>-<8 hex>` in 37 (what `new_run_id` mints); 3 hand-named |
| `trigger_source` | enum of 5 | 15 distinct values; 2 of them in the enum (`webhook` 12, `conductor` 15) |
| `models.teacher` / `.student` | objects (`provider`, `model_id`) | strings in 40/40; `judge` in 8; `additionalProperties: false` rejected it |
| `stages[*].status` | enum of 6 | 19 distinct strings: the driver writes `completed`, agents write their own (`complete` in 37 entries, `in_progress`, `launched`, `blocked`, `escalated_conflict`, `skipped_reused`, …) |
| `stages[*].outputs` / `.evidence` | array / string | objects in 12 manifests, as recently as the last run (2026-08-18); 2 entries are lists; 7 have no `status` |
| `stage_history`, `attestations`, `escalations` | absent | written by the driver (6, 1 and 2 manifests) |

The `stages` finding is a contract fact, not schema cosmetics: the map is written by the agents as
well as the driver, and only the driver's entries pass through `normalize_stage_complete`. The
corrected schema therefore types only `status` there, says so, and puts the types on
`stage_history`, which only the driver writes.

## The 4 that still fail, and why that is right

| Run | Created | Failure | Why |
|---|---|---|---|
| `run-phase2-main-0001` | 2026-07-28 | `run_id` pattern | written by hand before `start_pipeline` existed |
| `run-phase2-pilot-0001` | 2026-07-28 | `run_id` pattern | same |
| `run-v2-code-distill-0001` | 2026-07-30 | `run_id` pattern | same |
| `run-20260812T035446Z-dedaa965` | 2026-08-12 | `models` has `hf_repo`, `revision`, `license` | trigger `operator_relaunch_r6a_signed`: an operator relaunch that copied `plan.models` verbatim. `seed_manifest` keeps supply-chain keys out of `models`, and `test_a_seeded_manifest_validates[signed-plan]` seeds a plan carrying them to prove it |

Widening the schema to admit these would make it describe manifests nothing writes any more, which
is the defect being fixed.

## Offline proof

`tests/test_manifest_schema.py` (8 tests) builds manifests with the real writers — `seed_manifest`,
then `handle_stage_complete` (with a judge claim, so the driver attests) and
`_record_manifest_escalation` over an in-memory S3 — and validates the manifest the driver put back.
Seven schema mutations were each caught (old `run_id` pattern, old `trigger_source` enum, object
`teacher`, a role renamed, an extra required escalation key, `verified` retyped, a required
`started_at` on stage entries).
