# Verification: console hardening (Batch 1)

Intent chain: `intent/console-hardening/`. Date: 2026-10-28. Base: `main` at `95182dc`.
Detail (private, not in repo): `~/Downloads/llmops-security-audit/`.

## The leak, confirmed live before the fix

A read-only `GET /api/overview` against the deployed console returned the raw runs-table rows; of
the newest 10 runs, 5 carried a 652-character Step Functions `task_token` inside
`driver_beat_payload` (the value was counted, never recorded). `GET /api/run` returned the whole
manifest, including the signed approver's `approved_by`, `cognito_sub` and `source_ip`.

## Fixed

- `overview()` returns each run through `_public_run` (6-field allowlist); `run_detail()` returns the
  manifest through `_public_manifest` (per-stage `status`/`metrics`/`evidence`), keeping the full
  manifest local for the gate/OOD computation. Token, `driver_beat_payload`, `signed_gates`,
  `approval.*`, `params`, `plan`, `models` no longer leave the Lambda.
- Approver group required on the spend / standing-config / agent-behaviour POST routes
  (`APPROVER_ONLY_POSTS`, enforced centrally) and on `/api/tasks/<id>/accept`. Self-approval is not
  yet banned — one admin account both creates and approves; separation of duties waits for
  per-person accounts (the kept-Cognito decision), stated in code.
- `save_budget` rejects non-finite limits (a NaN budget never fires) and limits over a ceiling;
  `cost_v2` snaps `days` to {7,30,90} (caps the Cost Explorer fan-out); the 500 handler returns a
  generic message with a request id instead of raw exception text (which names account-bearing ARNs);
  two fragile `'${esc(x)}'` inline handlers use `jstr(x)`.

## Verified

- `tests/test_console_public_scope.py` (25 tests), with the run-row allowlist DERIVED from the
  frontend's `renderRuns` and the run-detail consumer checked to read the manifest only via `.stages`
  — a new frontend read of an unlisted field fails the test rather than silently re-widening the response.
- Existing console suite: 441 passed, unchanged. Full suite otherwise green (the 2 known
  sandbox-only failures — `test_capacity_race_guard.sh`, the stream-quiet driver test — fail the same
  way on a clean `main`).
- 6 negative controls, each caught (leak the row; leak the manifest; disable the approver guard;
  drop the accept check; accept a NaN budget; stop snapping `days`).

## Deliberately deferred

- A server-side cost estimate for `start-run` (now approver-only, so the group-less exploit is closed).
- Requiring auth on the cost GET routes.
- Separation of duties on accept — tied to introducing per-person Cognito accounts.
