# Agents — AgentCore Harness Configs

Five per-stage worker harnesses, a conductor, and a FinOps auditor for the LLMOps distillation
pipeline (teacher DeepSeek-R1 on Bedrock -> student Qwen3-1.7B on SageMaker). Each config is validated offline with
`deploy/validate_config.py`, created with `deploy/create_harness.py`, and gets the shared BYO
memory attached post-create by `deploy/04_wire_memory.py`.

| Stage | Agent | Harness | Skills (MLOps-agent-skills) | Inline functions | Tasks |
|---|---|---|---|---|---|
| 1 | [data-prep](data-prep/harness.json) | `llmops_data_prep` | llm-data-preparation, llm-prompt-engineering, llm-guardrails | stage_complete, checkpoint, escalate_human | verify, generate, curate |
| 2 | [finetune](finetune/harness.json) | `llmops_finetune` | llm-fine-tuning, llm-distillation, llm-cost-optimization | stage_complete, **job_launched**, checkpoint, escalate_human | prepare, launch, analyze |
| 3 | [eval](eval/harness.json) | `llmops_eval` | llm-evaluation, llm-guardrails | stage_complete, checkpoint, escalate_human | evaluate, gate |
| 4 | [deploy](deploy/harness.json) | `llmops_deploy` | llm-deployment, llm-cost-optimization | stage_complete, checkpoint, escalate_human | deploy, smoke, teardown |
| 5 | [monitor](monitor/harness.json) | `llmops_monitor` | llm-observability, llm-cost-optimization, llm-agent-orchestration | stage_complete, checkpoint, escalate_human | health, sweep, report |
| 6 | [orchestrator](orchestrator/harness.json) | `llmops_orchestrator` | llm-agent-orchestration, ml-solution-design, llm-cost-optimization | **launch_run, resolve_escalation, page_human, write_report**, checkpoint | plan, triage, report |
| — | [finops](finops/harness.json) | `llmops_finops` | llm-cost-optimization, ml-solution-design | **publish_cost_report, update_rate_card, flag_variance**, checkpoint, escalate_human | reconcile, pricing_refresh, report |

`llmops_finops` has **no stage number** because it is not in the state machine. It sits beside
`llmops_orchestrator` above it: the conductor decides what to spend, the auditor reports what
was spent, on a daily schedule that spans many finished runs. Its IAM is read-only on billing —
it reports and flags, and cannot stop a run. See [docs/COST.md](../docs/COST.md).

All agents: model `global.anthropic.claude-fable-5` with Opus 5 as the standing fallback
(vendor-quota failover — see AGENTS.md; no temperature/topP: Claude ≥ 4.7 rejects them),
`timeoutSeconds` 840 (driver Lambda 900s limit), sliding-window truncation (150 messages),
shell + code interpreter + skills; eval and monitor additionally get the managed browser for
console screenshot evidence. `job_launched` implements launch-and-release for SageMaker
training jobs (Step Functions `waitForTaskToken` resumes a fresh session on job completion).

**Dev vs prod variants:** `harness.json` (this directory) is the **dev** variant — PUBLIC
network, skills mounted from the git source (`timwukp/MLOps-agent-skills`, default branch).
The **prod** variant (`harness.prod.json`, added in Phase 6) uses VPC network mode with an
S3-mirrored skill snapshot, since VPC mode cannot reach GitHub and git skills float on main.
