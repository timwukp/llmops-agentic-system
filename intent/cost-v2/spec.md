# Spec: Cost v2 — price real usage, gate launches on budgets

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. `GET /api/cost/v2?days=N` (public read, like the other cost GETs) returns overview,
   usage, by-task, breakdown and detail in one payload.
2. Attributed spend is by explicit resource match only: training jobs whose names start with
   `COST_JOB_PREFIXES` (default `llmops-,arc2v2-`), billable seconds × the S3 rate card.
   Never "all SageMaker" — the account carries unrelated training jobs.
3. Bedrock token cost comes from CloudWatch `AWS/Bedrock` by `ModelId` and is labelled
   account-wide; it is never attributed to a task.
4. Cost Explorer billed figures are shown beside the estimates, never summed into them, and
   cached (each request bills $0.01).
5. Budgets: `project_monthly` or `per_task`, mode `notify` or `block`, stored as `budget#*`
   rows in the existing `LlmopsAdminRuns` table (no new AWS resource, no IAM change).
6. `budget_gate()` runs at `POST /api/start-run` and `POST /api/tasks/{id}/accept`. `block`
   breach → HTTP 409 naming the budget. `notify` breach → alert row + SNS publish, each
   wrapped so a failed notification never changes the gate's answer.
7. One source failing must not blank the tab: per-source errors are reported in the payload.
8. Existing v1 panels and routes stay working (their guards pin exact strings); they move
   under the Governance sub-tab. Removal is phase 2.

## Out of scope (phase 2)

Hard-stop of an in-flight run in the harness driver; AWS Budgets Actions; per-agent token
attribution; removing the v1 routes.
