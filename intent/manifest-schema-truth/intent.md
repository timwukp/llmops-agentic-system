# Intent: make the manifest schema describe the manifests

- **Slug:** manifest-schema-truth
- **Author:** Claude (AI agent)
- **Date:** 2026-10-13
- **Accepted-by:** Tim WU
- **Status:** accepted

## Problem

Phase 1 adds lakehouse provenance to the run manifest (research report §13, PR-01). Reading
`pipeline/contracts/manifest.schema.json` before extending it showed it disagrees with the code that
writes manifests: `run_id`, `trigger_source`, `models` and `stages[*].status` are all specified in a
form no writer produces. A live survey found 0 of 40 run manifests valid against it. Nothing validates
against the schema, so nothing noticed — and an extension built on it would inherit the drift.

## Desired outcome

The schema says what `start_pipeline` and the harness driver actually write, separates the
driver-verified blocks from the agent-writable `stages` map, and a test keeps it that way.

## Acceptance

- Manifests built by the real writers validate against the real schema, offline, in the suite.
- The validator is shown to reject what it should (mutation-checked).
- Live: the corrected schema is checked against every run manifest, and each remaining failure is
  explained in the evidence file.
