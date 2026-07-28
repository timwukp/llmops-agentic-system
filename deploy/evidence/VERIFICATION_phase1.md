# Phase 1 verification — data-prep harness live proof

Date: 2026-07-28 · Region: us-east-1 · All identifiers redacted per SECURITY.md.

## Gate

> data-prep harness: create → memory → observability → **invoke-verify**
> (lists mounted skills, calls SageMaker API, writes S3)

**Result: PASSED** — session `phase1-verification-run-0003-smoke-test`, streamed
to completion, zero runtime errors.

## What was proven live

| # | Check | Evidence |
|---|---|---|
| 1 | Harness created + READY | `llmops_data_prep` (id suffix redacted), model `global.anthropic.claude-fable-5`, 5 config versions, DEFAULT endpoint Ready |
| 2 | Skills actually mounted from git | Agent listed `.agents/skills/git/`: `llm-data-preparation`, `llm-prompt-engineering`, `llm-guardrails` — and described each correctly from its SKILL.md |
| 3 | Agent can operate SageMaker from its shell | `aws sagemaker list-training-jobs --max-results 3` exit 0, summarized 3 historical jobs |
| 4 | Agent can write S3 | Wrote `runs/phase1-verify/hello.json`; orchestrator-side `head_object` confirms 80 bytes, timestamps match |
| 5 | Shared memory attached + active | Console: 2 sessions, 10 create-events, 10 extracted memories, 0% error |
| 6 | Observability wired | APPLICATION_LOGS delivery to CloudWatch + TRACES to X-Ray active; `OTEL_TRACES_SAMPLER=always_on` set |

## Defects found live and fixed (self-iteration loop, 6 total)

| Defect | Root cause | Fix |
|---|---|---|
| `ParameterNotFound /llmops/iam/...` | 05_harnesses.py guessed a different SSM name than 01_iam.py published | read `harness_execution_arn` |
| `ValidationException` on GetHarness | `harnessId` requires the full `<name>-<10char>` id, not the bare name | resolve via list_harnesses, thread `harness_id` through |
| `ConflictException: while it is CREATING` | update issued before harness reached READY | wait_ready before update |
| Memory create rejected | episodic reflection namespace must equal/prefix the episodic namespace | `/episodes/{actorId}` |
| **`temperature` is deprecated for this model** | Bedrock rejects `temperature` for Claude ≥ 4.7 (Fable 5/Opus 5/Sonnet 5/4.8/4.7 — verified matrix) at INVOKE time only | removed from all configs + template |
| **`top_p` is deprecated for this model** | same class as above | removed from all configs + template |

Observability script additionally fixed (2 bugs): GetHarness response nests under
`harness`; PutDeliverySource rejects harness ARNs — must target the auto-created
runtime (`harness_<name>`) ARN. All fixes contributed back to the
agentcore-harness-builder skill (branch `fix/agentcore-live-verified-bugs`).

## Invocation transcript (redacted tail)

```
session: phase1-verification-run-0003-smoke-test
1. Skills listed — .agents/skills/git/: llm-data-preparation, llm-prompt-engineering, llm-guardrails
2. SageMaker API — list-training-jobs exit 0: 3 Completed jobs
3. S3 probe — s3://llmops-agentic-<ACCOUNT_ID>-us-east-1/runs/phase1-verify/hello.json
OK    harness responded. Configuration verified end to end.
```

Orchestrator-side verification (trust but verify):

```
$ aws s3 cp s3://<bucket>/runs/phase1-verify/hello.json -
{"phase": 1, "agent": "llmops_data_prep", "verified_at": "2026-07-28T15:12:44Z"}
size 80 · modified 2026-07-28T15:12:45Z   (1s after the agent's claimed write)
```
