# Phase 2 main verification — 24-task distillation dataset, generated + curated autonomously

Date: 2026-07-29 · Region: us-east-1 · Run: `run-phase2-main-0001` · Redacted per SECURITY.md.

## Gate

> curated.jsonl + stats in S3 (DeepSeek-R1 teacher via Bedrock)

**Result: PASSED.**

## Generation (24 ARC-AGI-2 training tasks, best-of-4 early-stop, 16k tokens)

Verified in S3 (`main_stats.json`, orchestrator-side read-back):

```json
{"tasks": 24, "solved": 8, "solve_rate": 0.3333, "total_attempts": 74,
 "total_input_tokens": 449769, "total_output_tokens": 925347,
 "wall_seconds": 2930, "cost_estimate_usd": 5.6,
 "teacher_model_id": "us.deepseek.r1-v1:0", "max_tokens": 16384,
 "strategy": "best-of-4-early-stop", "keep_reasoning": true}
```

- Bimodal pattern: solved tasks resolve in 1–2 attempts (3k–20k out tokens);
  unsolved burn the full 4×16k. Early-stop saved ~40% vs fixed best-of-4.
- Failures are stable genuine reasoning misses (same tasks failed identically
  across independent runs) — consistent with the pilot's 32k finding.

## Autonomous resilience upgrades (agent-initiated, mid-run)

1. A microVM recycle destroyed 9 local-only results → the agent switched to
   **per-task S3 checkpointing** (`checkpoints/results/`) on its own and recorded
   it in the manifest as standard practice. 23/24 checkpoints were in S3 at the
   next orchestrator poll; zero work lost after the change.
2. Sandbox lacks `ps`/`pgrep` and blocks `kill` → agent made its parallel
   workers **idempotent (skip-if-done)** instead of process-managed.
3. Survived 3 client-side stream disconnects (incl. a full laptop-close) across
   the run via same-session resume.

## Curation (5-stage, per llm-distillation methodology)

- **Verifiable-reward filter**: kept only the 8 exact-match-verified records —
  and re-verified every grid against ground truth at curation time rather than
  trusting the upstream flag. 16 wrong-answer records dropped.
- Format validation 8/8 · dedup 0 removals · TRL messages format with
  `<think>` reasoning kept · deterministic 6 train / 2 val split.
- Flagged forward: longest assistant trace ≈ 12k est. tokens → training
  `max_length` set to 14336.

Artifacts verified in S3 under `runs/run-phase2-main-0001/distillation/`:
`main_raw.jsonl` (1.0 MB) · `main_stats.json` · `curated_train.jsonl` (175 KiB,
6 records) · `curated_val.jsonl` (47 KiB, 2 records) · `curation_report.json`.

## Cost

Pilot $0.69 + main $5.60 ≈ **$6.29 total teacher spend** (within the ~$10–15 plan).

## Honest notes

- 6 train / 2 val is deliberately small for the first end-to-end pass — the
  point is proving the autonomous pipeline; scaling task_count is a parameter
  change in the manifest, not new engineering.
- Teacher solve rate 33% on ARC-AGI-2 matches expectations for this benchmark;
  gates downstream are RELATIVE (student vs teacher), per docs.
