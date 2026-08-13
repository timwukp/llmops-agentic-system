# Canonical RAFT context format

This file IS the format. The data-prep harness reads it from
`s3://<bucket>/code/eval/raft_context_format.md` (mirrored here by `deploy/03_storage.py
ensure_eval_instrument`, the same glob that mirrors the judge prompt) when it assembles
retrieval-aware training rows, and the eval harness reads the SAME key when it assembles
open-book inference prompts. Neither authors its own. Both record this file's `sha256` —
`raft_format_sha256` in `stats.json` (training side) and in `report.json` (inference side) —
so "training and inference used one format" is a digest comparison, not a claim.

## Why this file exists

The RAFT paper's own ablation is the warning: a model fine-tuned on retrieved context in one
format and queried with retrieved context in another scores WORSE than a model that never saw
retrieval at all (DSF+RAG under-performing DSF on HotpotQA, 4.41 vs 6.38). The failure needs no
bug — two agents each doing something reasonable produce it, which in this repo is the
"two correct halves, never connected" shape with a new coat of paint. One canonical file that
both halves are ordered to read verbatim, plus one digest recorded from both sides, is what
makes the drift impossible rather than unlikely.

## Change protocol

Same as the judge instrument's: editing this file is allowed — it is part of the experiment,
not a constant. What is NOT allowed is editing it silently: any change moves the digest, so
training rows built before the change and inference prompts built after it are two different
formats, which is exactly the mismatch this file exists to prevent. A run is internally
consistent iff the `raft_format_sha256` in its `stats.json` equals the one in its
`report.json`; state any change and the two digests in the evidence doc that spans it.

## The template

Substitutions, and there are exactly two: `{passage}` (one retrieved document's text, repeated
for each of the k documents, numbered from 1) and `{ticket}` (the bare ticket text). Nothing
else in the text below may vary between items, between runs, or between the training and
inference sides.

```text
Use the retrieved context documents below to resolve the ticket. Some documents
may be irrelevant; rely only on those that apply.

<retrieved_context>
[doc 1] {passage}
[doc 2] {passage}
</retrieved_context>

Ticket: {ticket}
```

The assembled text above is the ENTIRE user-turn content. On the training side it becomes
`messages[0].content` of the TRL chat row and the assistant target is the row's resolution
**verbatim, with no citation markup** — the judge compares answers blind, and citation clutter
in the answer would move `judge_score` for reasons that have nothing to do with resolution
quality. On the inference side it becomes the prompt handed to the student through the same
chat template the trainer used (`pipeline/v2/generate_student.py build_prompt`).

## Policy constants

These are format, not tuning knobs — they live here because both sides must agree on them:

1. **Oracle fraction 0.8**: 80% of training rows include the oracle passage (the retrieved
   document containing the row's own resolution) among the k documents; **20% carry
   distractors only**, so the student learns to say what the context supports rather than to
   trust that an answer is always present. (RAFT's p; their ablation puts the optimum at
   0.8–0.9 for domain QA.)
2. **Distractor count** comes from `params.retrieval_distractors` (plan-signed, default 3):
   retrieved-but-irrelevant passages from the same index, so distractors are hard rather
   than random.
3. **Document order is shuffled per row** — the oracle's position must carry no signal, or
   the student learns position instead of relevance.
4. **An empty retrieval is an empty block**, not an omitted one: a retrieve failure at
   inference renders `<retrieved_context>\n</retrieved_context>` and is counted in
   `retrieval_failures`. The student sees the same shape it trained on; the miss is a
   measured degradation of that item, never a format change and never an unscorable.
5. **The judge never sees any of this.** The judge instrument's prompt substitution is the
   bare ticket and its two reply substitutions are the answers alone — the bar (0.45)
   predates retrieval and must go on measuring the same thing.
