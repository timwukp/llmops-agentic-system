# Spec: one enforcement policy, two switches, a sweep

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. Policy row `cost-policy` in `LlmopsAdminRuns`: `block_launches`, `stop_running`, both
   default false; an unreadable policy reads as the default.
2. `POST /api/cost/policy` flips either switch; `llmops-approver` only (403 otherwise); every
   change writes an INFO alert naming who set what.
3. `budget_gate()` always alarms on a breach and refuses (409) only when `block_launches` is on.
   Budgets lose their per-budget mode.
4. `budget_sweep()` evaluates every budget: `project_monthly` thresholds alarm once per
   budget/threshold/month; `per_task` compares each running run's spend to the limit. Breached
   runs are stopped only when `stop_running` is on (`StopExecution` + `StopTrainingJob` for
   that run's in-progress jobs); otherwise one "not stopped" warning per run per month.
5. Entry points: EventBridge rule `llmops-budget-sweep` (`rate(15 minutes)`, Input
   `{"mode":"budget-sweep"}`) and `POST /api/cost/sweep`.
6. IAM `CostGuardrailStop`: exactly `states:StopExecution` (llmops-pipeline executions) and
   `sagemaker:StopTrainingJob` (`llmops-*`, `arc2v2-*`); no endpoint deletion anywhere.
7. Notifications never gate decisions (alert row and SNS wrapped separately).
8. Docs: COST.md §8 design (EN + zh-TW), READMEs, ARCHITECTURE (EN + zh-TW), console README,
   PROJECT_STATE, CHANGELOG, evidence.
