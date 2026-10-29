# Verification: cap agent-written reads (Batch 3, slice 1)

Intent chain: `intent/driver-hardening/`. Date: 2026-10-29. Base: `main` at `426c96e`.

## Changed

A harness agent can write a file of any size under its run prefix. These reads bounded it:
- `orchestration/harness_driver/handler.py`: `_get_capped` (Range read, `MAX_AGENT_JSON_BYTES` 4 MB);
  `_load_manifest` returns `{}` on oversize; `check_sweep_output` rejects; `_verify_gold_extract`
  streams via `iter_chunks` and refuses past `MAX_EXTRACT_BYTES` (256 MB), never holding it whole.
- `orchestration/conductor_tools.py`: `_read_s3_uri` reads under `MAX_PLAN_BYTES` (4 MB), raises past it.
- `orchestration/publish_provenance/handler.py`: `Reader.json` reads under `MAX_BYTES` (8 MB),
  returns None past it.

Each capped reader falls back to a plain read only when a test double raises `TypeError` for the
`Range` kwarg; real S3 always honours it, so production fetches are always bounded. gate.json was
already capped in Batch 0.

## Verified (offline)

- `tests/test_driver_read_caps.py` (10), caps shrunk via monkeypatch so the tests allocate nothing
  large; refusals exercised with a full-returning double so the length check (not a truncated-JSON
  parse failure) is what refuses.
- Existing driver/provenance/sweep/gate suites pass (730). Full suite otherwise green (2 known
  sandbox-only failures). 5 negative controls, each caught.

## Deferred to a later Batch 3 slice (hot-path, kept separate)

- Typed-call recovery scope + per-harness tool gating, bus-triage authenticity, one-approval-one-run
  on the driver path, turn/page caps, webhook replay protection.
