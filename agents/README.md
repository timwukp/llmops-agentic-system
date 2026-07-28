# Agents — AgentCore Harness Configs

Five per-stage worker harnesses for the LLMOps distillation pipeline (teacher DeepSeek-R1 on
Bedrock -> student Qwen3-1.7B on SageMaker). Each config is validated offline with
`deploy/validate_config.py`, created with `deploy/create_harness.py`, and gets the shared BYO
memory attached post-create by `deploy/04_wire_memory.py`.

| Stage | Agent | Harness | Skills (MLOps-agent-skills) | Inline functions | Tasks |
|---|---|---|---|---|---|
| 1 | [data-prep](data-prep/harness.json) | `llmops_data_prep` | llm-data-preparation, llm-prompt-engineering, llm-guardrails | stage_complete, checkpoint, escalate_human | verify, generate, curate |
| 2 | [finetune](finetune/harness.json) | `llmops_finetune` | llm-fine-tuning, llm-distillation, llm-cost-optimization | stage_complete, **job_launched**, checkpoint, escalate_human | prepare, launch, analyze |
| 3 | [eval](eval/harness.json) | `llmops_eval` | llm-evaluation, llm-guardrails | stage_complete, checkpoint, escalate_human | evaluate, gate |
| 4 | [deploy](deploy/harness.json) | `llmops_deploy` | llm-deployment, llm-cost-optimization | stage_complete, checkpoint, escalate_human | deploy, smoke, teardown |
| 5 | [monitor](monitor/harness.json) | `llmops_monitor` | llm-observability, llm-cost-optimization, llm-agent-orchestration | stage_complete, checkpoint, escalate_human | health, sweep, report |

All agents: model `global.anthropic.claude-sonnet-5` (converse_stream, temperature 0.2),
`timeoutSeconds` 840 (driver Lambda 900s limit), sliding-window truncation (150 messages),
shell + code interpreter + skills; eval and monitor additionally get the managed browser for
console screenshot evidence. `job_launched` implements launch-and-release for SageMaker
training jobs (Step Functions `waitForTaskToken` resumes a fresh session on job completion).

**Dev vs prod variants:** `harness.json` (this directory) is the **dev** variant — PUBLIC
network, skills mounted from the git source (`timwukp/MLOps-agent-skills`, default branch).
The **prod** variant (`harness.prod.json`, added in Phase 6) uses VPC network mode with an
S3-mirrored skill snapshot, since VPC mode cannot reach GitHub and git skills float on main.
