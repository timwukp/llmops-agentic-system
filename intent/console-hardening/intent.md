# Intent: the console does not leak live tokens or let any login spend or reconfigure

- **Slug:** console-hardening
- **Author:** Claude (AI agent)
- **Date:** 2026-10-28
- **Accepted-by:** Tim WU
- **Status:** accepted

Accepted on the owner's decision of 2026-10-28 to act on the security audit
(`~/Downloads/llmops-security-audit/`), Batch 1, after agreeing the platform must govern itself first.

## Problem

The console's read API is partly unauthenticated by design. Live on 2026-10-28, an anonymous
`GET /api/overview` returned the raw runs-table rows, which carry the Step Functions `task_token`
(also inside `driver_beat_payload`); `GET /api/run` returned the whole manifest, including the signed
approver's `approved_by`, `cognito_sub` and `source_ip`. A live token plus `states:SendTaskSuccess`
forges a stage's completion. Separately, several state-changing routes (`/api/start-run`,
`/api/cost/budgets`, `/api/cost/sweep`, the native-rec/optimize apply routes that rewrite a harness
prompt) checked only that the caller was logged in, not their group; `start-run` trusted a
client-supplied budget estimate; `save_budget` accepted a non-finite limit (a NaN budget never
fires); `/api/cost/v2?days=N` made each N its own cache key and billed Cost Explorer call; the 500
handler returned raw exception text (which names account-bearing ARNs).

## Outcome wanted

- The unauthenticated responses carry only the fields the page renders — no token, no approver identity.
- Spend / standing-config / agent-behaviour routes require the approver group.
- A budget limit must be finite; cost windows are a fixed small set; 500s are generic.

## Acceptance

- `tests/test_console_public_scope.py` (new) passes; the existing console suite still passes; negative controls.
- The field allowlist is derived from the frontend, not hand-copied.
