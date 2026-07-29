# Case study — what it took for agents to run an LLMOps pipeline alone

[繁體中文](CASE_STUDY.zh-TW.md) · [Architecture](ARCHITECTURE.md) · [Test results](TEST_RESULTS.md)

Everything in this document happened on a real AWS account and is recorded in
`deploy/evidence/VERIFICATION_phase*.md`. Nothing is a demo transcript; the
failures are kept in because they are the point.

## The goal

Replace the human LLMOps engineer for one complete lifecycle: a teacher LLM
(DeepSeek-R1 on Bedrock) generates training data, a student model (Qwen3-1.7B)
is QLoRA-fine-tuned on SageMaker, evaluated against quality gates, deployed to
an endpoint, smoke-tested, and torn down — **with a human pulled in only when
an agent calls `escalate_human`**. Not "an assistant that suggests commands":
six agents that hold the pager.

The total bill for proving it — six agents, a trained model, a deployed and
torn-down endpoint, five end-to-end iterations — was **≈ $12–15**, about one
hour of a human engineer.

## The thesis: three layers, not one model

The clearest single incident is from Phase 3. The finetune agent was asked to
launch a QLoRA training job; its S3 download of the training script failed with
a 403. With zero human intervention it: probed two prefixes and *induced* that
its IAM role was prefix-scoped (`runs/*` readable, `code/*` not) rather than
blindly retrying; searched fallbacks in priority order (local workspace → skill
directories → historical jobs' sourcedirs); found the sandbox had no `tar` and
rebuilt `sourcedir.tar.gz` with Python's `tarfile`; uploaded it to a prefix it
*could* write; submitted the job; verified `InProgress`; and released the
session with `job_launched`. Training started on the first human-free attempt.

That behavior is not a property of any one component. It is three layers
multiplied:

1. **Model quality** — each failure produced a designed hypothesis (a 2-point
   permission probe → "role is prefix-scoped"), a prioritized search order, an
   instant tool substitution. Weaker models retry the same 403 or give up.
2. **A real execution environment** — the AgentCore microVM (shell, filesystem,
   code interpreter) makes probing S3, building tarballs, and calling SageMaker
   real actions, not suggestions in a chat window.
3. **Engineered authorization** — every task prompt grants an explicit
   self-repair budget ("diagnose, fix, retry — max 3; then `escalate_human`"),
   and the mounted skills supply the domain shape of a correct fix. A
   conservatively-aligned model without that grant stops at the first 403 to
   ask a human.

Remove any layer and the same incident ends as `escalate_human: S3 403`
instead of a running training job.

## The remediation gauntlet — 6 training iterations

Getting one training job to `Completed` took six iterations, every failure
self-diagnosed (Phase 3 evidence):

| # | Failure | Diagnosis |
|---|---|---|
| 0 | `ImportError: torch>=2.1.1` | 2023 HF DLC too old for Qwen3 |
| 1 | CUDA OOM, 7.31 GiB at step 0 | 151k vocab × 14k ctx → fp32 logits ≈ 8 GiB on a 24 GB A10G → Liger fused CE (chosen over truncation, to preserve the longest verified trace) |
| 2 | liger needs transformers ≥ 4.52 | pin-floor conflict → raise the pin |
| 3 | `NameError: torch` *inside* transformers | silent degradation: transformers imports cleanly yet treats torch as absent when torch is below its floor → torch 2.6 DLC |
| 4 | bitsandbytes ≥ 0.46.1 required | exact pins were the disease, not the symptom → **strategy change**: floors-only requirements |
| 5 | — | **Completed** (431 s billable; train_loss 0.5013, eval_loss 0.5199) |

Process discipline held throughout: every iteration changed exactly ONE
variable with a written rationale; the full `remediation_history` is
append-only in the manifest. Iterations 1–3 were self-diagnosed by the
finetune agent within its 3-diagnosis budget; 4–5 were conductor-triaged —
the escalation protocol honored, not bypassed. And when a transient Bedrock
outage made the agent unreachable, the deterministic spine submitted the final
job itself (tagged `launched_by: orchestrator-fallback-bedrock-5xx`).

The payoff arrived in Phase 5: the finetune agent launched its next training
job **first-try**, using the floors-only + torch-2.6 recipe it had recorded to
the shared Memory. Six failures became one organizational learning.

## The honest gate — the model failed, and that's the proof

Phase 4 is the chapter most write-ups would hide. The pipeline deployed the
distilled student through five endpoint versions (four distinct root causes:
a config env parsed as a model URL, a legacy handler routing, a train/serve
transformers version skew, a packaging-layout miss — then `serving.properties`
at the tarball root, InService). The smoke test passed: the student answered a
rotation task correctly over HTTPS.

Then the quality gates ran, twice, on 16 held-out tasks — and the model scored
**0/16 both times**. The eval agent didn't soften it. It ran its own controls
(a lenient re-scan still found 0; control prompts through the identical path
returned well-formed grids, so the pipeline was sound) and produced the
diagnostic that explains everything: **`closed_think_rate` 0%** — no output
ever closed its `<think>` block; median generation 5,831 tokens; 12/16 hit the
context limit. The student had learned to *start* reasoning and never to
*converge*: the documented consequence of training on 6 traces, far below the
transfer floor for ARC reasoning into a 1.7B model.

The verdict stood. Nothing was "fixed" into a pass. And in Phase 5's hands-off
run, the honesty compounded: when the gate failed on a 2-sample mini-run
(`FAIL_CLOSED_NO_INPUT` — no quality signal exists at that scale), the state
machine armed the remediation loop, and the finetune agent *refused the
premise*: `REMEDIATE_PREMISE_INVALID — no quality signal to remediate` →
`escalate_human`. It declined to burn its iteration budget on an unfixable
premise. A platform for autonomous engineering is worth exactly as much as its
gates are hard to talk past — this one held against its own builders' model.

The gate is also mechanically fail-closed: a live defect (an agent emitting
`gate_passed: null` that an old default promoted to a pass) was fixed so that
only a literal `true` passes, with a regression test.

## Agent-initiated resilience upgrades

Nobody asked for these; the agents met a hostile environment mid-run and
redesigned around it (Phase 2 main evidence):

- **Per-task S3 checkpointing** — a microVM recycle destroyed 9 local-only
  results. The agent switched to checkpointing every task result to S3, on its
  own, and recorded it in the manifest as standard practice. 23/24 checkpoints
  were in S3 at the next poll; zero work lost after the change.
- **Idempotent parallel workers** — the sandbox lacks `ps`/`pgrep` and blocks
  `kill`, so process management was impossible. The agent made its workers
  skip-if-done instead of managed.
- **Self-diagnosed token truncation** — in the pilot, 7 of 8 outputs failed
  format validation. The agent read `stop_reason`, concluded the failures were
  truncations rather than wrong answers, recommended raising `maxTokens`
  (8k → 32k), and format validity went to 8/8.

## Vendor-quota failover

Phase 5 established a hard operational fact: model-vendor rate limits bind
even AWS-internal accounts, and a six-harness platform is its own token-flood
generator. ~12 Fable 5 5xx bursts recurred in one day — never as an explicit
throttle, always as `InternalServerException`/`ServiceUnavailableException`
while a single-shot probe of the same model succeeded. The response was to
make failover a design layer (AGENTS.md): a same-family fallback chain per
harness (Fable 5 → Opus 5, zero prompt changes, ~15 s hot-swap via
`UpdateHarness`, sessions surviving), mixed model allocation to spread quota
pressure, and driver-level auto-swap on the 5xx signature.

## What v1 proved — and what v2 will do

**Proved:** the full autonomous loop is real. Trigger → plan → generate →
curate → train (launch-and-release) → evaluate → deploy → smoke → teardown,
hands-off, with honest terminal states, cross-run learning through shared
Memory, self-remediation within explicit budgets, and escalation when — and
only when — the premise demands it. Every chain live-verified, ~$12–15 total.

**Not proved, by honest design:** a distilled student that *passes* its gates.
6 training traces cannot teach a 1.7B model to converge on ARC-class
reasoning; the Phase 4 evidence says so plainly, and the remediation ladder's
next rung is a design change, not a re-run.

**v2 — the recorded experiment:** attack the transfer floor on two axes.
*Code-as-reasoning*: distill the teacher's solutions as executable transform
programs rather than free-form `<think>` prose — a program either reproduces
the output grid or it doesn't, which makes every training example verifiable
and every trace convergent by construction (the exact property whose absence
`closed_think_rate: 0%` measured). *Augmentation at scale*: build on the prior
849-triplet dataset from the owner's earlier ARC work and expand it with
systematic transformations, replacing the 6-trace dataset with one above the
transfer floor. The platform side needs no new engineering: data scale is a
manifest parameter, and the first full-scale pass is the v2 experiment's
opening act.
