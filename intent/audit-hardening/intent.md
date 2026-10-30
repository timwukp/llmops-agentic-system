# Intent: untrusted transform code cannot exfiltrate credentials, and the durable stores cannot be silently lost or downgraded

- **Slug:** audit-hardening
- **Author:** Claude (AI agent)
- **Date:** 2026-10-30
- **Accepted-by:** Tim WU
- **Status:** accepted

Security audit Batch 5 (process isolation + storage hardening). Accepted under the owner's
"hurry up … finish all the project task and goal" of 2026-10-30, the same standing authorization as
Batches 2a/3/4. Offline-verifiable; the only deploy is `deploy/03_storage.py`, which is idempotent.

## Problem

Two findings, unrelated in subject but both "a control everyone assumed was there was not":

1. **The ARC verification sandbox is not a sandbox.** `pipeline/v2/verify_sandbox.py` executes
   model-written `transform(grid)` under a restricted-builtins namespace. That namespace is
   correctness scaffolding, not a security boundary: `().__class__.__bases__[0].__subclasses__()`
   and `np.__builtins__['__import__']('os')` are plain attribute walks no builtin filter can stop,
   and from `os` the code reaches the process's AWS credentials (env + the container credential
   endpoint + the IMDS token) and can exfiltrate or spend them. It can also allocate without bound
   and OOM the host.

2. **The durable stores had no in-transit deny, no point-in-time recovery, and no deletion
   protection.** The data bucket holds customer data, the KMS-signed approval records and the
   provenance cards, yet accepted plaintext (HTTP) requests. The DynamoDB tables — including
   `llmops-stage-events`, the audit trail that is the ONLY durable record of an escalation — could
   be dropped by a single `DeleteTable` and had no PITR, so an accidental or malicious delete was
   unrecoverable.

## Outcome wanted

- Untrusted transform code runs in a forked child whose environment carries no credential-shaped
  variable and whose address space is rlimited; it is killed if it overruns the wall clock. The
  default is isolated, so every existing call site is safe without editing it. A trusted,
  already-sandboxed caller can opt out with `isolate=False`.
- Every request to the data bucket is over TLS or denied. Every table the deploy manages carries
  deletion protection, and the ones whose loss is unrecoverable carry PITR — applied idempotently
  to tables that already existed, not only to ones created now.
- Legitimate transforms and a second deploy are unaffected.

## Acceptance

- `tests/test_audit_hardening.py` (new) passes; the existing sandbox and storage suites pass;
  five negative controls fire.
