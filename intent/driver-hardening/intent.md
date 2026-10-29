# Intent: an oversized agent-written file cannot OOM the driver or the Lambdas that read it

- **Slug:** driver-hardening
- **Author:** Claude (AI agent)
- **Date:** 2026-10-29
- **Accepted-by:** Tim WU
- **Status:** accepted

Security audit Batch 3 (first slice: resource exhaustion). Accepted on the owner's "carry on" of
2026-10-29. The dispatch-trust items (typed-call recovery scope, bus-triage authenticity,
one-approval-one-run, turn/page caps, webhook replay) are a later slice; they touch the hot path and
are kept separate.

## Problem

A harness agent can write a file of any size to its own run prefix. The driver and the
publish-provenance Lambda then read several of those files whole with `get_object()["Body"].read()`:
the manifest (every stage start and stage_complete), the sweep file, the Gold extract (hashed), the
conductor's plan (`conductor_tools._read_s3_uri`), and the provenance card's inputs. A multi-GB file
OOMs a 512 MB Lambda. An OOM is not catchable by the handler's `except`, so the task token never
settles, the stage hangs to its timeout, and async retries plus the resurrector replay it. gate.json
was already capped in Batch 0; the rest were not.

## Outcome wanted

- Every read of an agent-written file is bounded. JSON (manifest, sweep, plan, card inputs) is read
  under a few-MB cap via a Range request; the Gold extract is streamed and hashed with an upper bound,
  never held whole.
- Legitimate files are unaffected.

## Acceptance

- `tests/test_driver_read_caps.py` (new) passes; the existing driver/provenance/sweep suites pass;
  negative controls.
