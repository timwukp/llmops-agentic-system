# Intent: a lakehouse gate that measures something

- **Slug:** lakehouse-phase1-evidence
- **Author:** Claude (AI agent)
- **Date:** 2026-10-15
- **Accepted-by:** Tim WU
- **Status:** accepted

## Problem

PR-09's live lakehouse run ended borderline. The model scored 32/32 on `gold.qlora_eval`, but n=32 cannot
clear a 0.95 Wilson lower bound. The owner chose to rerun the gate on n≥100 items in PR-11. Measuring the
data first changed what "n≥100" has to mean. The generator builds conversations from one to two
templates per intent, so every eval row was a near-copy of a training row:
- seed 7's own eval split against its training rows: median closest-match similarity 0.935;
- another seed's rows against those training rows: 0.95.

Re-judging the same model on more generated rows would have measured template recall, not the model.
Shown that, the owner chose held-out question families plus a retrained model (2026-10-15).

## Desired outcome

Generator v2 adds paraphrase families: one policy answer per intent and language, with "train" and
"held_out" phrasings. Its acceptance set (`eval_prompts.jsonl`) holds 120 items at demo scale, each
with a reference answer and phrased only in held-out wording. The export bundles the acceptance set,
hashed; the review claims it; the driver re-measures it; eval anchors the gate to it. Then a live
ingest, a retrained model, and a gate verdict recorded as it falls.

## Acceptance

- No training conversation uses a held-out phrasing (planted contamination aside). The acceptance
  set's median closest-match similarity to training stays under 0.7 (measured at 0.538).
- At demo scale n ≥ 100, so a perfect score can clear a 0.95 lower bound.
- A live lakehouse run is judged on the acceptance set, its digest is attested by the driver, and the
  gate's verdict is recorded honestly.
