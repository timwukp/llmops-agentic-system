# Spec: process isolation + storage hardening (Batch 5)

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. **Sandbox** (`pipeline/v2/verify_sandbox.py`):
   - `_run_in_namespace(code, grid, timeout)` holds the existing in-process exec + SIGALRM path.
   - `_worker(code, grid, timeout, q)` runs in a forked child: it pops every credential-shaped
     environment variable (prefix `AWS_`, or containing `TOKEN`/`SECRET`, plus the two
     `AWS_CONTAINER_CREDENTIALS_*` names and the implicit IMDS reachability), sets `RLIMIT_AS` to
     `_WORKER_MAX_BYTES` (768 MB) and `RLIMIT_CPU`, then puts `_run_in_namespace(...)` on the queue.
   - `execute_transform(code, grid, timeout_sec=5, isolate=True)` forks via
     `multiprocessing.get_context("fork")`, joins with a grace margin, and `terminate()`→`kill()`s
     an overrun, returning `{"status": "timeout"}`; a child that dies with no result returns an
     error. `isolate=False` keeps the in-process path for a trusted caller.
   - The restricted namespace stays (it catches honest mistakes cheaply); it is explicitly NOT the
     boundary.
2. **Storage** (`deploy/03_storage.py`):
   - `ensure_bucket` attaches a `DenyInsecureTransport` bucket policy (`aws:SecureTransport=false`)
     covering the bucket and its objects. Idempotent put; dry-run attaches nothing.
   - `ensure_table` no longer early-returns on an existing table: it creates only when absent, then
     applies PITR (when `pitr=True`) and `DeletionProtectionEnabled=True` idempotently to the table
     whether it was created now or pre-dates this run. The `llmops-stage-events` caller passes
     `pitr=True`.
3. **Guards:** `tests/test_audit_hardening.py` (new) — the credential-scrub boundary proven by a
   real escape ([[0,0]] isolated vs [[1,1]] in-process), the timeout, a legitimate transform, and
   the bucket/table config; five negative controls in `tests/negative_controls/monitor_dispatch.py`.
