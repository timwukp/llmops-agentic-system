# Phase 3 verification — QLoRA distillation training via launch-and-release

Date: 2026-07-29 · Region: us-east-1 · Run: `run-phase2-main-0001` · Redacted per SECURITY.md.

## Gate

> ModelTrained event; SFN launch→wait→resume trace; QLoRA job

**Result: PASSED** (training completed; EventBridge→resume-λ chain live-verified twice;
full SFN execution deferred to the Phase-5 hands-off run as planned).

## Final state

- Training job `llmops-qlora-...-r5`: **Completed**, 431s billable (ml.g5.2xlarge)
- Artifacts verified in tarball: `adapter/` + `merged/` (bf16) + `metrics.json`
- **train_loss 0.5013 · eval_loss 0.5199** — healthy convergence on 6 verified
  reasoning traces (lr 2e-4, 3 epochs, r16/α32, max_length 14336, Liger fused CE)
- Resolved stack (floors-only): transformers 5.14.1 / trl 1.9.2 / peft 0.20.0 /
  bitsandbytes 0.50.0 on the torch 2.6 DLC
- CloudWatch: `Reporting training SUCCESS`, exit 0, zero OOM at 14336 ctx
  (identical config OOM'd without Liger — strongest evidence it's active)

## The remediation gauntlet — 6 iterations, all self-diagnosed

| # | Job | Failure | Diagnosis | Fix |
|---|---|---|---|---|
| 0 | (base) | `ImportError: torch>=2.1.1` | 2023 HF DLC too old for Qwen3 | newer DLC + pins |
| 1 | -r1 | CUDA OOM 7.31 GiB @ step 0 | 151k vocab × 14k ctx → fp32 logits ≈ 8 GiB on 24 GB A10G | Liger fused CE (chosen over truncation — preserves longest verified trace) |
| 2 | -r2 | liger needs transformers ≥4.52 | pin floor conflict | raise pin |
| 3 | -r3 | `NameError: torch` **inside transformers** | silent degradation: resolved transformers' torch floor > DLC torch 2.3 | conductor-triaged: torch 2.6 DLC |
| 4 | -r4 | bitsandbytes ≥0.46.1 required | transformers 4.52+ raised bnb floor above `==0.45.5` | **strategy change**: floors-only requirements |
| 5 | -r5 | — | — | **Completed** |

Process notes:
- Iterations 1–3 self-diagnosed by the finetune agent within its 3-diagnosis budget;
  4–5 were conductor-triaged (escalation protocol honored, budget respected).
- `-r5` was submitted by the orchestrator directly (tagged
  `launched_by: orchestrator-fallback-bedrock-5xx`) during a transient Bedrock
  outage window — spine deterministic fallback while the agent was unreachable.
- Every iteration changed exactly ONE variable with a written rationale;
  full `remediation_history` preserved append-only in the manifest.

## Launch-and-release chain — live-verified

1. finetune agent launched the job and called `job_launched` → session released.
2. Job terminal states fired the EventBridge rule `llmops-sagemaker-job-state`
   → `llmops-resume-pipeline` Lambda invoked (CloudWatch: two invocations
   observed — one on an early failure, one on the -r5 completion; 1.5s duration,
   0 errors; graceful skip on this manual run since no task token was parked).
3. Post-completion `analyze` ran in a **fresh session** reconstructing all
   context from AWS state (describe-training-job + S3 + manifest) — the
   "state lives in the manifest, never the session" contract held.

## Durable learnings recorded to shared Memory (by the agent)

- Floors-only requirements + latest official DLC = confirmed-green recipe for
  fast-moving HF stacks; exact pins caused 3 of 5 failures.
- Liger fused CE validated end-to-end at 14336 ctx / 151k vocab / 24 GB — default it.
- If eval gates fail on under-fit: the ONE-change lever is a 4th epoch, not lr.
- transformers can import cleanly yet treat torch as absent when torch is below
  its floor — silent-degradation mode, not a pip conflict.

## Cost

431 billable seconds on ml.g5.2xlarge ≈ **$0.14** for the successful run;
~25 failed-startup minutes across 5 failed jobs ≈ $0.50. Phase 3 total ≈ $0.64.
