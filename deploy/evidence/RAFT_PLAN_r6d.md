# r6d — RAFT retrieval run: design, pre-measurements, protocol

Written 2026-08-13, after #122–#125. Goal: the first e2e run that reaches `Complete` AND
passes the gate. Everything here is measured or pinned; the two paid steps at the bottom
wait on explicit human authorization.

## What changed and why (one paragraph)

r6c proved the closed-book dead end: correct decontamination deletes exactly the
org-specific facts the acceptance set demands (41% of rows), and scaling 1.7B→8B moved
OOD from 0.0 to 0.026 (`SCALING_DIAGNOSIS_r6c_8B.md`). The verified research pass
(`RESEARCH_r6_direction.md`) found no documented case of a sub-10B model passing a 0.45
blinded pairwise bar against human references without retrieval — and production evidence
that retrieval-aware fine-tuning cuts invented-org-entity rates 13.7%→1.9% at 7B. So r6d
moves the facts into a Bedrock KB (`deploy/09_retrieval.py`, #122), prices its standing
cost on the estimate (#123), fixes ONE canonical context format for training and
inference (#124), and wires the prompts to answer open-book with a judge that stays blind
(#125). Bar unchanged at 0.45: one experiment, one variable.

## Instrument digests this run is judged under

- `judge_prompt_pairwise.md` sha256 `9659d4a5ebe6454f7c52024228bdde9fb331e1807f3f3183e9019034a2ef228b`
  — byte-identical to r6c's offline judging (pinned by test); r6c's ID 0.2234 is the
  comparable baseline.
- `raft_context_format.md` v1 (this tree). `stats.json` and `report.json` must record the
  SAME `raft_format_sha256`, or the run is internally inconsistent by construction.

## Pre-measurement 1 — $0 recall probe (run 2026-08-13, `r6-relaunch/recall_probe.py`)

Lexical (trigram-Jaccard) top-5 retrieval over the 300-row corpus; a vector KB ranks at
least as well on paraphrase, so these are floors. Hit = a retrieved resolution reaches
Jaccard ≥ 0.6 against the item's reference answer (the pipeline's own decontamination
similarity rule).

| layer | n | recall@5 | best-Jaccard p50 | reading |
|---|---|---|---|---|
| id | 97 | **1.00** | 1.00 | every ID fact is stated in the corpus — verbatim |
| ood | 40 | **0.00** | 0.18 | the corpus does not state OOD facts at all |

**Gate (ID recall@5 ≥ 0.6): PROCEED.**

**What the 1.00 actually means — stated plainly, because it changes what the gate
measures.** The demo corpus's resolutions are heavily templated: 288 of 300 rows are
near-duplicates (resolution Jaccard ≥ 0.9) of some ID reference answer, and excluding all
of them leaves 12 rows with recall 0. So the org knowledge in this dataset exists ONLY as
(near-)verbatim canned resolutions; there is no independent restatement of the same facts.
Consequences, honestly framed:

1. Under RAFT, the ID layer measures **"can the system retrieve the org's canonical fix
   for a known ticket type and reproduce it faithfully"** — which is the support-bot
   product, and is exactly what ServiceNow's production numbers measure. It does NOT
   measure closed-book knowledge (r6c already measured that: 0.223) and it does NOT
   measure generalization.
2. Generalization is measured by the OOD layer — recall 0.00, so retrieval gives OOD
   nothing, the student is on its own there, and OOD stays **report-only** as designed.
   An OOD number that jumps would be evidence of contamination, not of skill.
3. This is not the acceptance FILE leaking into the index (that is refused at ingest and
   fenced by Retrieve-only IAM). It is the corpus stating the facts the acceptance set
   asks about — which is the entire design: the facts must live SOMEWHERE the student is
   allowed to look, or the gate is unpassable (r6c's conclusion, verbatim).

## Pre-measurement 3 — token audit (same probe, $0)

Assembled RAFT prompts at k=5, canonical format: p50 ≈ 895 tokens, p95 ≈ 996, max ≈ 1065
(at 3.6 chars/token). Inference window 8192: **fits with 7× margin**. Training
`--max_length` 14336: **fits with 13× margin**. Context length is not a risk for this
corpus; the curate-time token-stats escalation stays as a guard for real customer corpora.

## Pre-measurement 2 — base-8B+RAG probe (~$12–18, NOT YET RUN, needs authorization)

Assemble RAFT-format prompts for all 137 items (real retrieved context), generate with
BASE Qwen3-8B (one short g5 job, ~$1), judge on the offline 137-answer harness with the
canonical instrument (~$9–15 Opus tokens). Decision rule from the plan: ID judge_score
≥ ~0.40 → train (RAFT FT adds format fit + distractor robustness); < 0.30 → stop and fix
retrieval before any training spend. Given recall@5 = 1.00 with verbatim resolutions in
context, the base model's score is expected high; the probe still runs first because
"expected" is not a measurement.

## r6d run protocol (deploys are the human's action, in order)

1. `01_iam.py` (Retrieve grant) → 2. `03_storage.py` (mirrors the format file; judge
digest re-verified unchanged) → 3. `05_harnesses.py` update for data-prep/eval/orchestrator
→ 4. `09_retrieval.py --source-uri s3://<bucket>/customer-data/<task>/helpdesk_tickets.jsonl
--customer-eval-key <id key> --ood-eval-key <ood key> --ingest` (prints the standing-cost
line; ~$11.52/day from here) → 5. finops `pricing_refresh`.

Signed plan: `{pipeline_mode: full, source_uri, customer_eval_uri, ood_eval_uri,
retrieval_kb_id: <SSM /llmops/retrieval/kb_id>, retrieval_k: 5, retrieval_distractors: 3,
models: {student: Qwen/Qwen3-8B (mirrored), teacher, judge}, gates: {judge_score: 0.45,
format_validity: 0.95}, training_instance: ml.g5.2xlarge, max_iterations: 3,
kb_ocu_hours: <days-up × 48>}`.

Expected traversal: DataPrepGenerate → DataPrepCurate (RAFT rows + probe + decon counts)
→ FinetuneLaunch (~929 s) → FinetuneAnalyze → EvalGenerate (open-book) → EvalScore (blind
judge) → EvalGate (Wilson lower ≥ 0.45 decisive PASS) → QualityGateChoice → Deploy →
SmokeTest → MonitorHealth → Teardown → MonitorReport → **Complete**.

Post-run, same day: `09_retrieval.py --teardown` (stops the meter), PROJECT_STATE update,
finops reconcile prices the OCU-hours.

Budget (disclosed, never summed with other work): teacher fills $2–5 · training ~$0.4 ·
eval inference ~$2 · in-run judge $9–15 · probe judge $12–18 · agent tokens $20–40 · KB
$11.52/day × ~5 days ≈ $58. Worst case with 3 remediation iterations ≈ $150–200.

## Residual risks vs the 0.45 bar, updated by the probe

1. ~~Corpus coverage / retrieval miss~~ — **killed for ID** (recall@5 = 1.00); alive for
   OOD by construction, and OOD is report-only.
2. **Bar arithmetic**: decisive pass at n = 97 needs a point estimate ≈ 0.55. With
   verbatim resolutions in context the mechanism is there; the base+RAG probe converts
   "expected" into a number before GPU spend. A landing in ~[0.36, 0.55) is the
   borderline band → `page_human`, and a human "accept" reaching `Complete` is disclosed
   as a human-assisted pass, not the goal.
3. **Plumbing drift**: canonical file + two-sided digest + same-key guard (#124/#125);
   token audit passed with 7× margin; the base+RAG probe exercises the exact assembly
   end to end.
