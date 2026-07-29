# Phase 5 verification — full autonomy: triggers, conductor, hands-off e2e

Date: 2026-07-29 · Region: us-east-1 · Redacted per SECURITY.md.

## Gate

> hands-off e2e run: trigger → state machine → agents → honest terminal state

**Result: PASSED.** Five e2e iterations, each surfacing exactly one real defect
(fixed, regression-tested, redeployed); the final run traversed 7 states with
zero human intervention and ended in an HONEST terminal state chosen by the
agents themselves.

## The 6th agent — conductor (llmops_orchestrator)

- Created, memory-wired, obs+evals attached, plan-task verified live: given a
  natural-language goal + $40 budget it produced a 5-stage run plan with
  per-stage params, 6 executable-code gates, $29.09 estimate (+20% contingency
  = $34.91 ceiling), three-tier cost guardrails ($23 teacher cap → $34 triage →
  $40 abort), priced descope options, a 10-item assumptions block — and it
  flagged the biggest cost risk as "a leaked endpoint, not the estimate" plus
  an IAM gap (plans/* unwritable) which was then fixed.
- Inline functions: launch_run / resolve_escalation / page_human / write_report.

## Triggers (4)

| Trigger | State | Verified |
|---|---|---|
| EventBridge Scheduler | nightly cron, DISABLED by default | created |
| Webhook (HMAC) | API Gateway HTTP API `/webhook` | live: bad sig → 403, good sig → 202 + run started |
| Admin API | `POST /runs` (AWS_IAM auth) | route live |
| GitHub Actions | OIDC pattern (repo-side yml, Phase 6) | pending |

## Five e2e iterations — the self-iteration loop applied to the platform itself

| # | Reached | Defect found | Fix |
|---|---|---|---|
| 1 | DataPrepGenerate | Lambda roles lacked events:PutEvents on the custom bus | 3 roles extended |
| 2 | FinetuneLaunch | InvokeHarness takes harnessArn, NOT harnessId (unit-test fakes can't catch API contracts) | SSM name→ARN resolver in driver |
| 3 | DataPrepGenerate | mid-swap harness version propagation window hid inline functions | stabilize configs before runs; single model per fleet |
| 4 | **Deploy (7 states!)** | driver Lambda 900s vs 840s harness turns = ONE turn per invocation; Sandbox.Timedout killed a finished-but-unreported turn. ALSO: gate_passed=null was promoted by a fail-OPEN default | between-turn self-reinvoke (continuation payload); **gate fail-closed** (`is True` only) + regression test |
| 5 | RemediateFinetune → honest EscalateFail | — none — | — |

Run 5's terminal sequence is the platform working as designed:
- eval agent (2-sample mini run): `gate_passed: false, verdict: FAIL_CLOSED_NO_INPUT`
  — no quality signal exists at this scale, said so plainly.
- state machine → RemediationChoice → RemediateFinetune (loop armed correctly).
- finetune agent: `REMEDIATE_PREMISE_INVALID — no quality signal to remediate`
  → escalate_human. **It refused to burn iterations on an unfixable premise** —
  exactly the honest-over-busy behavior the design demands.
- Zero orphaned endpoints (Deploy never entered); 4 stage_complete events in DDB;
  training cost $0.14.

## Chains now proven stable (2+ consecutive autonomous successes)

- webhook → start-pipeline → manifest seed → SFN execution
- SFN → driver → InvokeHarness stream → toolUse⇄toolResult → verify → token settle
- launch-and-release: job_launched → token parked → EventBridge job-state rule →
  resume λ → send_task_success → machine advances (2× hands-off)
- shared-Memory learning transfer: finetune agent launched -r0 first-try with the
  floors-only + torch-2.6 recipe learned in Phase 3's remediation gauntlet

## Vendor-quota reality (design consequence)

Fable 5 5xx bursts recurred across the day (~12); owner confirmed vendor rate
limits bind even AWS-internal accounts. All six harnesses currently run Opus 5
(hot-swapped via UpdateHarness, ~15s each). Failover-as-design codified in
AGENTS.md; automatic driver-level failover is Phase-6 hardening.

## Honest scope notes

- A run reaching PipelineCompleted end-to-end requires meaningful data scale
  (the 2-sample mini run cannot pass an evidence-based gate by construction);
  that full-scale pass is the v2 experiment's opening act.
- GitHub Actions trigger lands with Phase 6 (repo-side workflow).
