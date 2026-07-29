# Phase 4 verification — deploy, quality gates, teardown

Date: 2026-07-29 · Region: us-east-1 · Run: `run-phase2-main-0001` · Redacted per SECURITY.md.

## Gate

> evaluation/report.json gates decided; endpoint smoke + teardown

**Result: PASSED as a pipeline** — every stage executed and verified live.
**The model itself FAILED its quality gates** — and that verdict standing (not
being "fixed" into a pass) is the strongest possible evidence the gate works.

## Deploy — 5 versions to InService, 4 distinct root causes

| Version | Failure | Root cause | Layer |
|---|---|---|---|
| v1 | crash-loop, stuck Creating | `SERVING_LOAD_MODELS=default` (orchestrator-dictated env; DJL parsed the literal as a model URL) | config |
| v2 | Python handler died at init | `ROLLING_BATCH=disable` routes lmi15 to the legacy HF handler | serving path |
| v3 | tokenizer parse crash | training stack (transformers 5.14) wrote `extra_special_tokens` (list); container's older transformers can't read it | **train/serve version skew** |
| v4 | vLLM engine initialized 22× but DJL never registered | env-only config: DJL engine detection scans tarball ROOT, model was in `merged/` | packaging |
| **v5** | — | `serving.properties` at tarball root (canonical LMI config) | **InService** |

Smoke test (direct invoke): rotation task answered correctly — the distilled
student does grid reasoning over HTTPS. Two platform limits discovered:
synchronous InvokeEndpoint has a hard **60s timeout** (long-CoT requires
`invoke_endpoint_with_response_stream`), and the streaming path returns clean
decoded text (the sync path exhibited a BPE-marker artifact).

## Quality gates — evaluated honestly, twice

16 held-out ARC-AGI-2 tasks (training tasks 25–40; never seen in training).
Teacher baseline: DeepSeek-R1, same tasks, 3/16 (18.75%).

| Iteration | Budget | Student solve | Format validity | Gate |
|---|---|---|---|---|
| 0 | 2048 tokens (sync) | 0/16 | 18.75% | FAILED |
| 1 | 7000 tokens (streaming; ceiling = max_model_len 8192 − prompt) | 0/16 | 18.75% | **FAILED — final** |

Why the verdict is trustworthy (eval agent's own controls):
- Lenient re-scan (first grid / after ANSWER / after `</think>` / any-grid):
  still 0 — not an extraction artifact.
- Control prompts through the identical client path returned coherent reasoning
  and well-formed grids — pipeline, template, and parser are sound.
- Diagnostic metrics: `closed_think_rate` **0%** (no output ever closed its
  `<think>` block), median generation 5,831 tokens, 12/16 stopped by context
  limit. The student learned to START reasoning but never to CONVERGE.

Root cause: **6 training traces are far below the transfer floor** for ARC
reasoning into a 1.7B student — exactly the documented expectation ("validates
the PIPELINE; competition-grade ARC performance is out of scope"). The
remediation ladder's next rung (training-data scale) is a design change, not a
re-run — recorded as the v2 experiment (code-as-reasoning + augmentation).

## Teardown — zero orphans

- v5 deleted after the verdict (≈56 min InService ≈ $0.94). All 5 model records
  + 5 endpoint-configs removed (agent deleted what its role allowed and flagged
  `requires_operator_cleanup` for the rest — completed by operator).
- Agent-reported deviations (honesty over apparent completion): List*/DeleteModel
  are denied to the harness role → teardown planned around known-name deletion;
  flagged rather than silently claimed.
- Out-of-scope find reported to the owner: an unrelated endpoint InService since
  2024-04 (~27 months of billing) — surfaced, deliberately untouched.

## Cost (Phase 4 total ≈ $4)

~3.9 endpoint-hours across the 5-version arc (incl. concurrent v1-wait +
v2-create window) + eval teacher tokens (reused Phase-2 checkpoints where
possible).

## What goes back to the skills (task list #17)

Env-vars-vs-serving.properties, lmi15 rolling-batch routing, train/serve
transformers-family coordination, 60s sync limit → streaming for long CoT,
Creating-is-undeletable cost trap, teardown-by-known-name under least privilege.
