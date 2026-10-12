# Spec: what goes, what stays

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. Remove routes `GET /api/cost-overview`, `GET /api/cost-estimates`, `POST /api/cost-estimate`,
   `POST /api/cost-approval-request`, `POST /api/cost-approval` and the functions only they
   used; remove `start_run`'s `estimate_id` gate.
2. Remove `BUDGET_MODE`: the reference is advisory-only; `accept_task` has no approval-queue
   branch; the signed record keeps its `gate` fields (`conductor_tools.SIGNED_KEYS`).
3. Keep `/api/finops-run`, `rate_card_health` (served in `/api/cost/v2` detail, rendered on the
   Detail sub-tab), `gate_decision`, `project_to_date_usd`, the shadow estimate write.
4. Narrow the console grant on `llmops-cost-estimates` to `PutItem` on the table.
5. Tests of removed behaviour deleted; replacements for the behaviour that survives; negative
   controls whose code is gone retired, any re-pointed control verified by running its mutation.
6. Docs updated in EN + zh-TW.
