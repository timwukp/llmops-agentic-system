# Intent: no answer states an identifier the question never gave

- **Slug:** identifier-grounding
- **Author:** Claude (AI agent)
- **Date:** 2026-10-26
- **Accepted-by:** Tim WU
- **Status:** accepted

Accepted on the owner's chat decision of 2026-10-26: "好，先做第 2 個，修捏造識別碼", after the owner asked
whether the fine-tuned answers hallucinate ("就是說 fine-tune 後的答案，有 hallucination？").

## Problem

On the Outcome tab every fine-tuned answer of the 2026-10-23 demo run ended in a case number nobody
asked about: "（案件編號 C00089）", "(case C00148)". Measured:
- **The training data taught it.** All 234 Gold training answers end in a case reference; no question
  contains one (`pipeline/lakehouse/generate.py` appends `C{i:05d}` to every agent reply, as a CRM
  does). A model that cannot know the real number learns to coin one.
- **The gate cannot see it.** `answer_match` checks that the reference is contained in the answer, so
  an invented suffix still scores a match; the run passed at 72.5% with a fabricated identifier in
  every one of its answers.
- **Real data has the same shape.** Ticket, case and return numbers are assigned by systems after the
  fact and appear in transcripts the customer never typed.

## Outcome wanted

- **Data:** Silver removes a system-assigned reference the customer never gave from the training
  answer; Gold's data-quality rules refuse one.
- **Evaluation:** a canonical deterministic scorer measures `identifier_grounding`, the share of
  answers with no identifier absent from the question, with a Wilson interval; the eval agent runs it
  verbatim and the demo gates it.
- **Visible:** the provenance card shows it as rows, like every gate metric.

## Acceptance

- Offline guards and negative controls.
- The 2026-10-23 model re-judged through the pipeline with the new gate fails it.
- A new build-then-train run through the pipeline passes it.
