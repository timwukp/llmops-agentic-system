# Phase 2 pilot verification — autonomous distillation data generation

Date: 2026-07-28 · Region: us-east-1 · Run: `run-phase2-pilot-0001` · Redacted per SECURITY.md.

## What was proven

The **data-prep harness autonomously executed a full distillation data-generation
cycle** — including a self-driven remediation iteration — with the orchestrator only
supplying the task and relaying its own recommendation back:

1. Cloned the official ARC-AGI-2 dataset (github.com/arcprize/ARC-AGI-2) inside its microVM.
2. Built teacher prompts for 8 training tasks (grids rendered as JSON rows).
3. Called teacher **DeepSeek-R1 via Bedrock serverless** (`us.deepseek.r1-v1:0`),
   4 calls in parallel from its shell, capturing `reasoningContent` + final text.
4. Validated predicted grids EXACTLY against ground truth (verifiable reward).
5. Wrote `pilot_raw.jsonl` (213 KB, full reasoning traces) + `pilot_stats.json` to the
   run's S3 prefix and appended its stage entry to the manifest.

## Self-iteration observed live (the /goal loop, at data level)

| Iteration | maxTokens | Solve rate | Format validity | Diagnosis by the agent |
|---|---|---|---|---|
| 1 | 8,192 | 1/8 | 1/8 (7 truncations) | "failures are truncations, not wrong answers — R1 exhausted the budget inside reasoningContent; recommend ≥16-32k" |
| 2 | 32,768 | 2/8 (25%) | **8/8 = 100%** (gate ≥0.95 met) | "remaining failures are genuine reasoning misses; recommend best-of-n sampling for the main run" |

The agent diagnosed its own failure mode from `stop_reason` evidence, recommended the
fix, executed it on instruction, and recorded durable learnings in the manifest
(16k budget likely sufficient; `--cli-read-timeout 0` needed for long R1 calls;
best-of-4 ≈ $8.50 for 24 tasks).

## Orchestrator-side verification (trust but verify)

```
$ aws s3 ls s3://<bucket>/runs/run-phase2-pilot-0001/distillation/
2026-07-28  213242 pilot_raw.jsonl
2026-07-28     179 pilot_stats.json
$ aws s3 cp .../pilot_stats.json -
{"tasks": 8, "solved": 2, "solve_rate": 0.25, "total_input_tokens": 22590,
 "total_output_tokens": 59859, "wall_seconds": 732, "iteration": 2, "maxTokens": 32768}
```

## Resilience patterns validated live

- **Same-session salvage**: two client-side stream interruptions (read timeout);
  both times the harness kept working server-side and the session resumed with zero
  lost work — including one teacher call it detected as killed and retried itself.
- **Cost**: merged final ~$0.35; cumulative incl. iteration-1 waste ~$0.69.

## Honest notes

- 25% teacher solve rate on ARC-AGI-2 is expected — this validates the PIPELINE;
  competition-grade performance is out of scope (docs state this).
- The `llm-cost-optimization` skill is NOT mounted on data-prep (by design — it's on
  finetune/deploy/monitor); the agent flagged it couldn't cross-check pricing. Good
  honesty signal from the skill prompts.
- Client fix applied: `invoke_harness.py` now uses `read_timeout=870, retries=0`.
