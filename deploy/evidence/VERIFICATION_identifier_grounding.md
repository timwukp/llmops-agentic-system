# VERIFICATION — identifier grounding, in the data and in the gate (2026-10-26)

## What the owner asked

"就是說 fine-tune 後的答案，有 hallucination？", then "好，先做第 2 個，修捏造識別碼".

## The finding, measured

- **On the Outcome tab** (2026-10-23 demo run `run-20261023T160741Z-65fec5d8`), every fine-tuned
  answer shown ended in a case number the question never gave: "（案件編號 C00089）", "(case C00148)".
- **The training data taught it.** The run's Gold `train.jsonl`: 234 rows; in all 234 the assistant
  turn ends in a case reference and the user turn contains none. Source:
  `pipeline/lakehouse/generate.py` appends `C{i:05d}` to every agent reply (as a CRM does), and the
  comment there says why: it is what keeps two clean conversations from being exact copies.
- **The gate could not see it.** `answer_match` checks that the reference is contained in the answer.
  The run passed at 72.5% with a fabricated identifier in every answer.
- **The new scorer on that run's 120 answers:** `identifier_grounding` **0/120** (Wilson 95% upper
  bound 3.1%). Order numbers the question gave (`TW10305`) are grounded and not flagged.

## Found while fixing it

- **The case number was hiding duplicates.** With it removed, template-mates became the same pair:
  at the demo scale 283 pairs, 266 distinct, so `pair_id` was no longer unique. Hence
  `exact_dup_after_strip`.
- **A gate of 0.99 could never pass on 120 items.** Wilson lower bounds: 120/120 0.969, 119/120
  0.9543, 118/120 0.9413. The gate is 0.95: at most one fabricated identifier in 120.
- **`\b` misses an identifier glued to CJK text** (a CJK character is a word character to Python's
  `re`), so the scorer bounds identifiers by ASCII letters and digits.
- **The guard that pins the eval instrument set fired:** the score step itself must name the
  scorer's S3 URI, not only a clause elsewhere in the prompt. Fixed, and the harness redeployed
  before the re-judge reached `EvalScore`.
- **The redaction census fired on the Wilson constant's digits:** the 97.5% normal quantile written
  out to 16 places contains a 12-digit run. It is now computed with `NormalDist().inv_cdf(0.975)`.

## Deployed (2026-10-26, from branch `claude-and-human/identifier-grounding`)

No pipeline execution was RUNNING at the first deploy.

| Step | Result |
|---|---|
| `03_storage.ensure_eval_instrument` (the eval mirror alone; the full script would also re-upload contracts from a branch without #182) | `code/eval/` read back byte for byte; `identifier_grounding.py` sha256 `11e75a1a…`; `judge_prompt_pairwise.md` unchanged (`9659d4a5…`) |
| `10_lakehouse.py` | Glue jobs updated; `lakehouse/glue/transform.py` and `generate.py` byte-equal to the branch |
| `05_harnesses.py --agent eval` | `llmops_eval` v18, then v19 (the score-step fix), READY, `config_confirmed` |

The two runs below passed their gates explicitly through start-pipeline. The console preset's gate
was deployed afterwards, once this branch carried #182 (a console deploy from a branch without it
would have removed #182's reader): `deploy/console/deploy.sh` (owner's terminal); the live package's
`provenance.py`, `lambda_function.py` and `frontend.html` byte-equal to the branch, and the demo
preset carries `"identifier_grounding": 0.95`.

## Live 1: the 2026-10-23 model re-judged with the new gate

Dispatched through `llmops-start-pipeline`: `run-20261026T133101Z-c266a595`, `eval_only`, the
2026-10-23 run's trained artifact, its held-out acceptance set, gates
`{format_validity 0.95, answer_match 0.6, identifier_grounding 0.95}`.

**SUCCEEDED** (`eval_only` stops at the verdict): PipelineModeChoice → EvalGenerate → EvalScore →
EvalGate → EvalOnlyStopChoice → Complete. `gate.json`:

| Gate | Result | Wilson 95% | Threshold | Verdict |
|---|---|---|---|---|
| format_validity | 120/120 | [0.969, 1.0] | 0.95 | decisive_pass |
| answer_match | 87/120 (72.5%) | [0.639, 0.797] | 0.6 | decisive_pass |
| **identifier_grounding** | **0/120** | **[0.0, 0.031]** | 0.95 | **decisive_fail** |

**gate_passed: false.** The same model that passed the old gates on the same questions is stopped by
the new one. `report.json` records `identifier_scorer_sha256 11e75a1a…`, equal to the mirrored
`code/eval/identifier_grounding.py`, so the agent ran the canonical scorer verbatim; it wrote the 120
ungrounded items to `evaluation/identifier_grounding.json`.

## Live 2: a build-then-train run on the fixed data

Dispatched through `llmops-start-pipeline`: `run-20261026T135417Z-9af35a3d`, `lakehouse`, generator
seed **71659** and scale demo (the same raw data as the 2026-10-23 run, so the comparison is the rule's
alone), the same three gates.

**SUCCEEDED**, every stage: LakehouseGenerate → Land → Silver → Gold → Export → Review →
FinetuneLaunch → FinetuneLaunchFull → FinetuneAnalyze → EvalGenerate → EvalScore → EvalGate → Deploy →
SmokeTest → MonitorHealth → Teardown → MonitorReport → **PublishProvenance** → Complete. The first run
in which `PublishProvenance` ran inside the pipeline rather than as a backfill.

**Data** (`lakehouse/reports/<dataset>/`):
- Silver: `strip_ungrounded_ids` 283 (every pair), `exact_dup_after_strip` 24, so **259 pairs**; PII
  redactions 30; DQ passed.
- Gold: 205 train / 24 eval; DQ **14/14**, including the new rule refusing a case reference.

**Gate** (`gate.json`), against the 2026-10-23 run on the same raw data (seed 71659):

| Gate | 2026-10-23 (case numbers in training) | **This run** |
|---|---|---|
| answer_match | 87/120 = 72.5% [0.639, 0.797] | **85/120 = 70.8%** [0.622, 0.782], pass |
| format_validity | 120/120 | **120/120** [0.969, 1.0], pass |
| identifier_grounding | 0/120 (re-judged: decisive_fail) | **120/120** [0.969, 1.0], **pass** |

`identifier_scorer_sha256 11e75a1a…`, the canonical scorer. The answer_match difference lies inside
both intervals; the fabricated identifiers went from every answer to none.

**A fourth `gate.json` shape.** This run's eval agent wrote the verdicts under `gates_applied`
(`ci_low`/`ci_high`, `n`, a `detail` string). The provenance reader reported it `unknown`, as
designed, so the first card had no gate rows. The reader now names that shape; the publisher was
redeployed and the card re-published: model version **2** of `llmops-student-store-copilot-lakehouse`,
`confirmed`, 16 metric rows (each gate with its Wilson bounds and threshold, `gate.identifier_grounding`
1.0 / 0.969 / 1.0 / 0.95 among them), approval still **PendingManualApproval**.

Four shapes from one prompt is itself the finding: the eval agent should write `gate.json` against a
declared schema, checked when it is written, rather than every reader learning each shape afterwards.
A follow-up.
