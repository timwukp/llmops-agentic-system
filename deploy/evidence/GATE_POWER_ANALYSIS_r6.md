# Gate Power Analysis — the numeric basis for the r6 plan's quality gate

**Date:** 2026-08-11 · **Data:** r5 (`run-20260811T101948Z-f9d34d27`) `evaluation/judge_details{-i0,-i1}.jsonl`, `student_outputs{,-i1}.jsonl` — 80 blinded pairwise judgments (2 iterations × 40), positions balanced 20A/20B per iteration. All numbers below are recomputable from those S3 artifacts; the analysis commands are in this file's appendix.

## What r5's own data says

| Quantity | i0 | i1 | pooled |
|---|---|---|---|
| W / T / L | 0 / 0 / 40 | 0 / 0 / 40 | 0 / 0 / 80 |
| win-rate 95% upper bound (rule of 3) | 7.5% | 7.5% | 3.8% |
| student/reference median length ratio | 0.77 | 0.80 | — |
| student answers longer than reference | 5/40 | 3/40 | 8/80 |

Three conclusions the gate design must respect:

1. **The current student is genuinely far below any defensible bar.** 0/80 pooled puts the true rate below 3.8% with 95% confidence. The scaling experiment (1.7B vs 4B vs 8B) is justified; no gate re-tuning can honestly pass this student on this eval set.
2. **Verbosity is NOT the failure mode here** — the student is 20-23% *shorter* than the references it loses to. Length-controlled correction would not rescue r5, and with zero outcome variance the LC (GLM) coefficient **cannot be fitted at all** until wins/ties exist. LC therefore stays out of the gate until pooled multi-run data with outcome variance exists (it remains a required *recording* obligation — lengths must be in judge_details from r6 on).
3. **Position debiasing is operating as designed** (20/20 splits both iterations) — keep it.

## Why n=40 cannot gate honestly

Minimum detectable effect at 80% power / 5% α (worst-case Bernoulli s²=0.25), and Wilson 95% CI half-width at p=0.5:

| n | MDE | CI half-width |
|---|---|---|
| 40 | 22.1pp | ±14.8pp |
| 100 | 14.0pp | ±9.6pp |
| 150 | 11.4pp | ±7.9pp |
| 200 | 9.9pp | ±6.9pp |
| 500 | 6.3pp | ±4.4pp |
| 1000 | 4.4pp | ±3.1pp |

Power of an n=40 gate to show a true-0.55 student clears a 0.50 bar: **15.4%** — a coin flip weighted against the student. (Reference: Miller, *Adding Error Bars to Evals*, arXiv 2411.00640, Eq. 9; the 969-questions-for-3pp figure from that paper is impractical here, so we size for a wider honest margin instead.)

## Recommended r6 gate design

- **Metric:** `judge_score` = (wins + 0.5·ties) / n — ties are a first-class outcome (AlpacaEval practice), not folded into losses.
- **Decision rule** (replaces the prose "within 0.05 → escalate" band): pass decisively iff the Wilson 95% **lower** bound ≥ bar; fail decisively iff the **upper** bound < bar; otherwise `escalate_human` with the numbers. This is the same escalation philosophy the prompt already had, made exact.
- **Bar = 0.45, ID acceptance set n = 100–150** (sampled from the 12 training categories, held out via the existing `customer_eval_uri` decontamination path). Under this design a true-0.60 student passes with **92% power at n=100, 98% at n=150**; a true-0.50 student escalates rather than silently failing.
- **OOD layer:** the existing 40-row category-disjoint set stays, as `params.ood_eval_uri` — measured and reported in `report.json.ood.*`, **never gated**. Its n=40 is fine for reporting-with-CI; it is only too small to gate on.
- `format_validity ≥ 0.95` unchanged (r5 scored 1.0 both iterations — this gate works).

## Appendix — recompute

```bash
aws s3 cp s3://<bucket>/runs/run-20260811T101948Z-f9d34d27/evaluation/judge_details-i1.jsonl - | \
  python3 -c "import json,sys; from collections import Counter; print(Counter(json.loads(l)['result'] for l in sys.stdin))"
```
MDE: `(1.96+0.8416)*sqrt(0.25/n)` · Wilson: standard two-sided at z=1.96 · power: one-sided normal approximation, α=5%.
