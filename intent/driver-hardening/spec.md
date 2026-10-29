# Spec: cap agent-written reads (Batch 3, slice 1)

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. **Driver** (`orchestration/harness_driver/handler.py`): `_get_capped(s3, bucket, key, max)` does a
   Range read (at most max+1 bytes) and reports too_big; `MAX_AGENT_JSON_BYTES` (4 MB) and
   `MAX_EXTRACT_BYTES` (256 MB). `_load_manifest` returns `{}` on oversize (its existing empty-on-error
   contract); `check_sweep_output` returns a rejection; `_verify_gold_extract` streams via `iter_chunks`
   and refuses past the cap. Each capped reader falls back to a plain read only when a test double
   raises `TypeError` for the `Range` kwarg (real S3 always honours it).
2. **Conductor** (`orchestration/conductor_tools.py`): `_read_s3_uri` reads the plan under
   `MAX_PLAN_BYTES` (4 MB) and raises past it.
3. **Provenance** (`orchestration/publish_provenance/handler.py`): `Reader.json` reads under
   `MAX_BYTES` (8 MB) and returns None past it (its existing missing-is-None contract).
4. **Guards:** `tests/test_driver_read_caps.py` (caps shrunk via monkeypatch so the tests allocate
   nothing large); negative controls on each cap.
