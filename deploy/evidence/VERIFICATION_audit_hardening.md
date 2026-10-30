# Verification: process isolation + storage hardening (Batch 5)

Intent chain: `intent/audit-hardening/`. Date: 2026-10-30. Base: `main` at `5766d04`.

## Changed

Two audit findings, both "a control everyone assumed was present was not":

- **`pipeline/v2/verify_sandbox.py`** — model-written `transform(grid)` ran under a
  restricted-builtins namespace, which is not a security boundary (`np.__builtins__['__import__']`
  and the `__subclasses__()` walk reach `os`, hence the process's AWS credentials and the IMDS
  token, and can allocate without bound). `execute_transform(..., isolate=True)` now runs the code
  in a forked child whose environment has every credential-shaped variable scrubbed (`AWS_*`,
  `*TOKEN*`, `*SECRET*`, the two `AWS_CONTAINER_CREDENTIALS_*` names) and whose `RLIMIT_AS`
  (768 MB) and `RLIMIT_CPU` are capped; the parent kills an overrun. The restricted namespace stays
  as cheap mistake-catching, explicitly not as the boundary. `isolate=False` keeps the in-process
  path for a trusted, already-sandboxed caller; the default is isolated, so every call site is safe
  unchanged.
- **`deploy/03_storage.py`** — `ensure_bucket` attaches a `DenyInsecureTransport` policy
  (`aws:SecureTransport=false`) over the bucket and its objects (the data bucket holds customer
  data, the KMS-signed approval records and the provenance cards; plaintext is never legitimate).
  `ensure_table` no longer early-returns on an existing table: it creates only when absent, then
  applies PITR (when asked) and `DeletionProtectionEnabled=True` idempotently — to tables that
  pre-date this run as well as ones created now, which is where a pitr-on-create-only path silently
  missed `llmops-stage-events`, the only durable record of an escalation. That table is now
  deployed with `pitr=True`.

## Verified (offline)

- `tests/test_audit_hardening.py` (13). The isolation boundary is proven by a **real** escape, not
  a mock: the same transform that reaches `os.environ` returns `[[1,1]]` run in-process (the
  credential is visible — so the test is not a tautology) and `[[0,0]]` run isolated (scrubbed). A
  legitimate transform still runs, an infinite loop is killed to `timeout`, and the credential
  scrub removes a secret under any key, not just the well-known name. The storage tests exercise
  `ensure_table` for a new table, an existing table (still gains the protections), the no-PITR
  case, and dry-run; and `ensure_bucket` for the TLS deny shape and dry-run.
- Existing sandbox + storage suites pass (`tests/test_verify_sandbox.py`,
  `tests/test_lakehouse_storage.py`, and the downstream augment/held-out-gate suites).
- Full suite: **2546 passed**, 2 known sandbox-only failures (`capacity_race_guard.sh` cannot run
  under the OS sandbox; the quiet-at-the-wall driver test hits `mktemp` EPERM) — identical on
  `main`. **5 negative controls** (551 pairs total), each caught: isolation-off-by-default,
  scrub-neutered, TLS-deny-inverted, deletion-protection-dropped, stage-events-PITR-dropped.

## Live step (owner)

`deploy/03_storage.py` is idempotent and offline-verifiable; no canary needed. On the next run the
existing tables gain PITR + deletion protection and the data bucket gains the TLS deny. The sandbox
change ships with whatever bundles `pipeline/v2`.
