# Test results — consolidated evidence

[繁體中文](TEST_RESULTS.zh-TW.md) · [Architecture](ARCHITECTURE.md) · [Case study](CASE_STUDY.md)

Every claim below traces to a verification file in `deploy/evidence/` — each one
the record of real invocations on a real AWS account (identifiers redacted per
SECURITY.md). "Always invoke before declaring success" is the repo rule; this
page is its ledger.

| Phase | Gate | Result | Evidence file |
|---|---|---|---|
| 0 — scaffold | preflight + config validation + unit tests + offline dry-runs | ✅ | CI + [PROJECT_STATE.md](../PROJECT_STATE.md) |
| 1 — spine proof | data-prep harness invoke-verified live | ✅ PASSED | [VERIFICATION_phase1.md](../deploy/evidence/VERIFICATION_phase1.md) |
| 2 pilot — data generation | autonomous distillation cycle + self-remediation | ✅ PASSED | [VERIFICATION_phase2_pilot.md](../deploy/evidence/VERIFICATION_phase2_pilot.md) |
| 2 main — dataset | curated.jsonl + stats in S3 | ✅ PASSED | [VERIFICATION_phase2_main.md](../deploy/evidence/VERIFICATION_phase2_main.md) |
| 3 — training | ModelTrained via launch-and-release | ✅ PASSED | [VERIFICATION_phase3.md](../deploy/evidence/VERIFICATION_phase3.md) |
| 4 — eval + deploy | gates decided; endpoint smoke + teardown | ✅ PASSED as a pipeline (model FAILED its gates — see below) | [VERIFICATION_phase4.md](../deploy/evidence/VERIFICATION_phase4.md) |
| 5 — autonomy | hands-off e2e: trigger → state machine → agents → honest terminal state | ✅ PASSED | [VERIFICATION_phase5.md](../deploy/evidence/VERIFICATION_phase5.md) |
| FinOps — cost governance | 7th runtime live; every rate carries its provenance; nothing publishes a guess | ⚠️ PARTIAL — runtime + arithmetic proven; rate card blocked on an IAM apply | [VERIFICATION_finops.md](../deploy/evidence/VERIFICATION_finops.md) |

## Static and offline checks (repeatable, CI-enforced)

| Check | Result | How to reproduce |
|---|---|---|
| Unit tests (contracts, cost model, driver loop, Lambdas, state machine document) | **637/637 passed** | `.venv/bin/python -m pytest tests/ -q --ignore=tests/golden` |
| Harness config validation (5 specialists + conductor + auditor) | **7/7 `RESULT: OK`** | `python deploy/validate_config.py --config agents/<a>/harness.json` |
| Architecture SVG geometry (no wire crossings, no wire through a card) | **CLEAN** | `python tests/check_svg_geometry.py docs/architecture-*.svg` |
| Redaction scan (account IDs, credentials, account-bearing ARNs) | CLEAN | `.github/workflows/redaction-check.yml` |

## Live invocations per phase

| Phase | What ran on real AWS | Key verified facts |
|---|---|---|
| 1 | data-prep harness created → memory → observability → invoke-verify | skills listed from git mount; `aws sagemaker list-training-jobs` exit 0; S3 write confirmed orchestrator-side (`head_object`, 80 bytes, 1 s skew); memory active (2 sessions, 10 extracted memories, 0% error); logs + X-Ray delivering. 6 live defects found and fixed in the same loop (incl. `temperature`/`top_p` deprecation for Claude ≥ 4.7 — surfaced only at INVOKE time) |
| 2 pilot | 8 ARC-AGI-2 tasks distilled via DeepSeek-R1 (`us.deepseek.r1-v1:0`) | agent self-diagnosed token truncation from `stop_reason` (8k → 32k: format validity 1/8 → 8/8); `pilot_raw.jsonl` 213 KB verified in S3; 2 stream interruptions salvaged same-session |
| 2 main | 24-task generation + 5-stage curation | `main_stats.json` read back from S3: 8/24 solved, 74 attempts, best-of-4 early-stop (~40% token savings); curation re-verified every grid against ground truth, dropped 16 wrong-answer records; final 6 train / 2 val |
| 3 | QLoRA training (ml.g5.2xlarge) via launch-and-release | job Completed, 431 s billable; train_loss 0.5013 / eval_loss 0.5199; artifacts (adapter + merged bf16 + metrics.json) verified in the tarball; EventBridge → resume-Lambda chain observed twice (1.5 s, 0 errors); zero OOM at 14336 ctx with Liger fused CE |
| 4 | deploy → smoke → quality gates → teardown | endpoint v5 InService after 4 root-caused failures; smoke test answered a rotation task correctly over HTTPS; gates evaluated twice, FAILED honestly (below); teardown zero orphans (5 models + 5 endpoint-configs removed) |
| 5 | conductor + triggers + 5 hands-off e2e iterations | conductor produced a costed 5-stage plan from a natural-language goal ($29.09 estimate, 3-tier cost guardrails); webhook live (403/202); final e2e run traversed 7 states, zero human intervention, honest terminal state |

## The e2e gauntlet — 5 iterations, one real defect each (Phase 5)

| # | Reached | Defect found | Fix |
|---|---|---|---|
| 1 | DataPrepGenerate | Lambda roles lacked `events:PutEvents` on the custom bus | 3 roles extended |
| 2 | FinetuneLaunch | InvokeHarness takes `harnessArn`, NOT `harnessId` (unit-test fakes can't catch API contracts) | SSM name→ARN resolver in the driver |
| 3 | DataPrepGenerate | mid-swap harness version propagation window hid inline functions | stabilize configs before runs; single model per fleet |
| 4 | Deploy (7 states) | driver Lambda 900 s vs 840 s harness turns = one turn per invocation; `Sandbox.Timedout` killed a finished-but-unreported turn. Also: `gate_passed=null` promoted by a fail-open default | between-turn self-reinvoke (continuation payload); **gate fail-closed** (`is True` only) + regression test |
| 5 | RemediateFinetune → honest EscalateFail | — none — | — |

Run 5's terminal sequence is the platform working as designed: eval said
`FAIL_CLOSED_NO_INPUT` (a 2-sample mini-run has no quality signal), the machine
armed the remediation loop correctly, and the finetune agent answered
`REMEDIATE_PREMISE_INVALID — no quality signal to remediate` → `escalate_human`
rather than burning iterations. Zero orphaned endpoints; 4 `stage_complete`
events in DynamoDB; training cost $0.14.

## The quality gate that honestly failed (Phase 4)

16 held-out ARC-AGI-2 tasks (training tasks 25–40, never seen in training);
teacher baseline DeepSeek-R1 on the same tasks: 3/16 (18.75%).

| Iteration | Budget | Student solve | Format validity | Gate |
|---|---|---|---|---|
| 0 | 2,048 tokens (sync) | 0/16 | 18.75% | FAILED |
| 1 | 7,000 tokens (streaming) | 0/16 | 18.75% | **FAILED — final** |

Why the verdict is trustworthy (the eval agent's own controls): a lenient
re-scan of outputs still found 0 solves (not an extraction artifact); control
prompts through the identical client path returned coherent, well-formed grids
(pipeline and parser are sound); and the diagnostic that explains it —
**`closed_think_rate` 0%**: no output ever closed its `<think>` block, median
generation 5,831 tokens, 12/16 stopped by the context limit. The student
learned to *start* reasoning but never to *converge* — the documented
consequence of 6 training traces, far below the transfer floor for ARC
reasoning into a 1.7B student. The pipeline verdict PASSED; the model verdict
FAILED; neither was adjusted to flatter the other.

## Cost

| Item | Cost |
|---|---|
| Phase 2 (teacher tokens: pilot $0.69 + main $5.60) | **$6.29** |
| Phase 3 (431 s successful training ≈ $0.14 + ~$0.50 failed-startup minutes) | **≈ $0.64** |
| Phase 4 (~3.9 endpoint-hours across the 5-version arc + eval teacher tokens) | **≈ $4** |
| Phase 5 mini-runs | **≈ $1** |
| **Total, all phases** | **≈ $12–15** |

The entire test-proven record — six agents, a trained model, a deployed and
torn-down endpoint, five e2e iterations — cost about as much as one hour of a
human LLMOps engineer.

## FinOps — the 7th runtime, and what two failures verified (2026-07-31)

Full record: [VERIFICATION_finops.md](../deploy/evidence/VERIFICATION_finops.md).

| Check | Result | How to reproduce |
|---|---|---|
| Unit tests (all suites, incl. `test_cost_model.py` + `test_finops.py`) | **637 passed** | `.venv/bin/python -m pytest tests/ -q` |
| Harness config validation, 7 agents | **`RESULT: OK`** | `python deploy/validate_config.py --config agents/finops/harness.json` |
| Live fleet | **7 harnesses READY** | `list_harnesses` via the repo's vendored boto3 |
| Canonical module has a distribution path | prints `would upload 4 contract files` | `python deploy/03_storage.py --region us-east-1 --account-id 123456789012 --dry-run` |

Every guard added in this work was **mutation-checked**: the asserted behaviour was
reverted one at a time and the test confirmed to fail. A test that passes both with and
against the behaviour it names is not a test.

### Two failures worth more than the passes

**Denied billing reads.** The auditor reported *"Priced SKUs: 0. Unpriced: all"*, stated
the existing card's freshness as **unknown** rather than assuming, **declined** to call
`update_rate_card`, and named the exact missing permissions.

**Denied S3 and its own arithmetic module.** It derived a complete **37-SKU rate card with
`unpriced: []`**, then refused to publish it — stamping its own output
`v1-DRAFT-noncanonical` because the `fallback_static` tier lives inside the module it
could not reach, and saying so in its first line.

The failure mode that would have mattered is a confident-looking card nobody can
regenerate next month. **Someone approves a $2000 run on these figures.** Fail-closed held
under conditions nobody designed for.

### Rate provenance, measured

CE-realized and Price List agree to **<0.001%** on the 5 SKUs both carry — realized rates
are trustworthy as primary. But every Anthropic entry in Price List for us-east-1 is
`Claude 2.0 · Claude 2.1 · Claude 3 Haiku · Claude 3 Sonnet · Claude Instant`: **no
Fable 5, no Opus 5 — the harness fleet's own LLM usage, the largest AgentCore line.** A
Price-List-only refresh silently zero-prices it.

A planning finding was **corrected** here: Price List *can* price DeepSeek-R1. The `model`
attribute value is bare **`R1`** (`provider=DeepSeek`), so scanning 84 values for a name
containing "DeepSeek-R1" finds nothing and wrongly concludes absence.

### Four defects that only deploying could find

Each was repo-complete, documented, unit-tested — and would never have been created:
a harness config absent from `AGENTS`; a scheduled function with no `LAMBDAS` entry
(a daily `ResourceNotFound` visible only in the scheduler's own metrics);
`update_function_configuration` never passed `Role`, so a role change reached only
functions that don't exist yet (**measured**: `"updated"` reported while the live function
kept its birth role); and an execution role with no `finops/*` grant.

**Not yet published:** the rate card and estimator validation against the measured
**$10.77** ground truth are blocked on an IAM apply. Everything above is `provisional` by
CE's own `Estimated: true` flag — publishing it as settled is the one thing the prompt
forbids.
