# Infrastructure record — what runs where, on what hardware

Live-verified 2026-07-29 (run `run-phase2-main-0001`). This is the factual record of
the compute forms and hardware behind each pipeline stage — kept because "which form
of compute and why" is a design decision, not an accident.

## The three compute forms, by stage

| Stage | Compute form | Why this form |
|---|---|---|
| Teacher generation (distillation data) | **Bedrock serverless** (`us.deepseek.r1-v1:0` via `bedrock-runtime converse`) | Zero instances, zero endpoint management, per-token billing; data never leaves the AWS account. No notebook, no GPU to own. |
| Agent runtimes (all 7 harnesses) | **AgentCore Harness microVMs** (CPU-only, managed) | The agents orchestrate; they never need GPUs themselves. |
| Student training (QLoRA SFT) | **SageMaker Training Job** (`create-training-job`, script mode) | Batch workload: instance spins up, trains, releases — billed 431s for the successful run. NOT a notebook (interactive, human-attended — wrong for autonomy) and NOT an endpoint (that's serving). |
| Student inference (eval gates + smoke) | **SageMaker real-time endpoint** (DJL-LMI + vLLM) | Eval must measure the production path (HTTPS invoke, real p50 latency). Torn down after gates — no standing cost. |
| Lambdas / Step Functions / EventBridge | Serverless spine | Deterministic control flow needs no LLM and no GPU. |

## Hardware detail

### Training — `ml.g5.2xlarge` (job `llmops-qlora-run-phase2-main-0001-r5`)

| Item | Value |
|---|---|
| GPU | 1 × NVIDIA A10G, 24 GB GDDR6 |
| vCPU / host DRAM | 8 vCPU / 32 GiB |
| Instances | 1 (single node) |
| Parallelism | **None** — no tensor/pipeline parallelism, no per-GPU layer split |
| Precision | 4-bit NF4 base (QLoRA) + bf16 compute; Liger fused CE |
| Wall/billable | 431 s |

Why single-GPU suffices for a 1.7B student: NF4-quantized base ≈ 1.2 GB; LoRA
adapters (r=16, α=32, 7 projection targets: q/k/v/o/gate/up/down) train ≈ 0.5% of
parameters; the real memory hazard was the **fp32 CE logits tensor
(14,336 seq × 151,936 vocab ≈ 8 GB)** — eliminated by `use_liger_kernel=True`
(fused linear cross-entropy never materializes full logits; proven by the r2 OOM
→ r5 zero-OOM delta). Multi-GPU layer splitting (pipeline parallelism) becomes
relevant at ~7B+ full-finetune scale — deliberately out of scope for a distilled
student; right-sizing is the point of distillation.

### Inference — `ml.g5.xlarge` (endpoint `llmops-student-run-phase2-main-0001-v4`)

| Item | Value |
|---|---|
| GPU | 1 × NVIDIA A10G, 24 GB GDDR6 |
| vCPU / host DRAM | 4 vCPU / 16 GiB |
| Instances | 1 |
| GPU parallelism | **None** — whole model on one GPU (merged bf16 weights ≈ 3.4 GB) |
| Serving stack | DJL-LMI 0.33 (lmi15, cu128) + vLLM rolling batch |
| Concurrency | batch-level: `MAX_ROLLING_BATCH_SIZE=4` (4 concurrent requests share the GPU via vLLM continuous batching — NOT 4 GPUs) |
| Memory budget | `GPU_MEMORY_UTILIZATION=0.85` (~20.4 GB: weights + KV cache), `MAX_MODEL_LEN=8192` |
| Cost | ~$1.006/hr while live; deleted after eval gates |

### Teacher — no hardware at all

DeepSeek-R1 via Bedrock serverless. Per-token pricing ($1.35/M in, $5.40/M out);
total teacher spend for pilot + main ≈ $6.29. The alternative (self-hosting a
reasoning-class teacher on SageMaker) would need multi-GPU instances at $10+/hr —
serverless teacher is a deliberate architecture decision, not a shortcut.

## Version-skew lesson (train stack vs serve stack)

Training resolved transformers 5.14 (floors-only), which writes
`extra_special_tokens` (a list) into `tokenizer_config.json`; the LMI container's
older transformers crashes parsing it (`'list' object has no attribute 'keys'`).
Fix: patch the artifact (remove the key) — but the durable rule is:
**pin the SERVING stack's transformers family in mind when choosing the TRAINING
stack's floors, or post-process artifacts for the serving container.** Recorded
for the llm-deployment/llm-fine-tuning skill feedback PRs.

## FinOps — the cost of knowing the cost

The auditor is the cheapest component in the platform and the only one that runs on a schedule
regardless of whether a pipeline run happens.

| Item | Cost |
|---|---|
| `llmops_finops` per invocation | ~$0.05–0.15 (Fable 5 tokens + AgentCore vCPU/GB-hours) |
| Daily 09:00 UTC reconcile | **~$1.5–4.5/month** — the recurring commitment |
| Cost Explorer / Price List / Budgets reads | $0 (read-only APIs, within free request tiers) |
| Two DynamoDB tables (on-demand) | cents/month at this write volume |

The per-invocation figure is not the number that matters. A daily schedule is a **monthly
subscription**, and that is the one to judge — the same reasoning the estimator applies to a
run's `worst_case_usd` rather than its expected total.

For scale: this project's whole month-to-date spend was **~$10–15** when the auditor was added,
against an account total of **$27,491** belonging mostly to unrelated work. That ratio is why
attribution is by explicit resource match and never by service — see [COST.md](COST.md).

