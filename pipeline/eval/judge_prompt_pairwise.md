# Canonical pairwise judge instrument

This file IS the measuring instrument. The eval harness reads it from
`s3://<bucket>/code/eval/judge_prompt_pairwise.md` (mirrored here by `deploy/03_storage.py
ensure_eval_instrument`) and uses it verbatim. It does not author its own.

## Why this file exists

Before it, the pairwise instrument had **no home**. `agents/eval/harness.json` said "fixed judge
prompts" without fixing one, and the mirrored `llm-evaluation` skill implements only a 1-5
absolute score — so the pairwise prompt was re-authored, from scratch, on every run. Two runs'
`judge_score` numbers were therefore not strictly comparable, and nothing in the pipeline could
tell you whether they were measured with the same ruler.

That is not hypothetical. r5's report carried `judge_ties: 0`, which read as "the judge found no
ties in 40 items" and was used as evidence that the student was uniformly worse. It was an
artifact of the prompt that run happened to author: it offered only A-or-B. Offered `tie` as a
first-class verdict on the same task, **this** instrument ties on 38% of in-distribution items
(36 of 94, measured 2026-08-12, `deploy/evidence/SCALING_DIAGNOSIS_r6c_8B.md`). A 38-point shift
in the reported tie rate came from the instrument, not the student.

So: one file, mirrored to S3, and its `sha256` recorded in every `report.json` as
`judge_prompt_sha256`. Comparability stops being a claim and becomes a check — two runs are
strictly comparable iff their recorded digests are equal, and a reader can verify that without
trusting anyone's memory of what the prompt said.

## Change protocol

Editing this file is allowed and expected — it is a scientific instrument, not a constant. What
is NOT allowed is editing it silently: any change moves the digest, so every run before the
change and every run after it become two incomparable populations. State the change and the two
digests in the plan or the evidence doc that spans it, and never pool the numbers across it
without saying you did.

## The prompt

Substitutions, and there are exactly four: `{task_description}` (one clause from the plan naming
what is being answered, e.g. "IT helpdesk ticket"), `{prompt}`, `{a}`, `{b}`. Nothing else in the
text below may vary between items or between runs.

```text
You are grading two candidate replies to the same {task_description}.

Ticket:
{prompt}

Reply A:
{a}

Reply B:
{b}

Which reply better resolves the ticket? Judge on: technical correctness, whether the
steps actually address the reported symptom, completeness of the resolution, and fitness
as a first-line reply to the user. A longer reply is not a better reply -- do not reward
verbosity, padding, or restating the ticket. If the two replies are of genuinely
equivalent quality, or differ only in wording, answer "tie": a tie is a real verdict,
not a failure to decide.

Answer with JSON only, no other text:
{"winner": "A" | "B" | "tie", "reasoning": "<one or two sentences>"}
```

## Invariants the caller must hold

1. **Temperature 0**, and the student's answer occupies position A for half the items and
   position B for the other half — or, as measured here, every item is judged in BOTH positions.
   Position balancing is what makes the score a property of the answers rather than of the
   ordering: measured on the 8B run, 6 of 94 in-distribution items flipped verdict when the
   positions were swapped, and 0 of 39 out-of-distribution items did.
2. **The verdict set is exactly {A, B, tie}.** A tie is recorded as a tie and credited 0.5; it is
   never folded into a loss (see r5 above) and never dropped.
3. **The reply being graded against is the customer's reference answer**, not the teacher's — the
   deliverable is agreement with the customer's answers.
4. **`maxTokens` must be large enough for the judge to finish thinking.** A judge model that
   reasons by default bills its reasoning as output tokens: at `maxTokens: 400`, 30 of 274
   (11%) judge calls spent the whole budget inside `reasoningContent` and returned an EMPTY text
   block with `stopReason: max_tokens`. 2000 was sufficient for every call on the same data. An
   empty text block is not a tie and not an undecided judge — see below. Record each call's stop
   reason in `judge_details.jsonl` so a truncated judge is visible rather than inferred.

## Unscorable items: not a tie, not a loss, not silently gone

A judge call can fail to produce a verdict for reasons that have nothing to do with the answers:
the platform's content filter blocks the call, the model returns unparseable text, or the budget
above truncates it. Measured on the 8B run: **9 of 274 (item, position) slots were
`content_filtered`, 3.3%, across 7 items, nondeterministically** — retrying recovered 5, and 4
items stayed unjudgeable at every attempt (`id#79`, `id#86`, `id#93`, `ood#2`).

The failure mode this creates is a shrinking denominator nobody notices. Rules:

1. Retry an unscorable call (the filter is nondeterministic; 5 of 9 recovered). If it still
   fails, mark the item `unscorable` with the reason.
2. **`judge_n` counts scored items only, and `judge_unscorable` / `judge_unscorable_ids` are
   reported next to it.** An unscorable item may not be counted as a tie, a loss, or a win.
3. The missingness is **not random**, which is the part that matters: the 4 permanently
   unjudgeable items clustered on credential / MFA / access content — the same categories where
   the student scored 0.000. Dropping them is therefore optimistically biased, and a confidence
   interval computed on the survivors understates the uncertainty rather than widening to
   cover it.
4. So the gate must ask whether the missingness could change its own verdict, rather than
   applying a fixed tolerance: recompute the decision with every unscorable item imputed as a
   win, then as a loss, then as a tie. **If the verdict (pass / fail / borderline) is the same
   under all imputations, proceed** — the missing items cannot change the outcome. **If it
   differs, call `escalate_human`** with the imputed verdicts, because the answer then depends on
   items the instrument could not read.

Worked example, from the 8B run's own numbers (bar 0.45, Wilson 95%):

| layer | imputation | n | W | T | L | judge_score | CI | verdict |
|---|---|---|---|---|---|---|---|---|
| ID | as scored | 94 | 3 | 36 | 55 | 0.2234 | [0.151, 0.318] | FAIL |
| ID | unscorable = win | 97 | 6 | 36 | 55 | 0.2474 | [0.172, 0.342] | FAIL |
| ID | unscorable = loss | 97 | 3 | 36 | 58 | 0.2165 | [0.146, 0.308] | FAIL |
| ID | unscorable = tie | 97 | 3 | 39 | 55 | 0.2320 | [0.159, 0.325] | FAIL |
| OOD | as scored | 39 | 1 | 0 | 38 | 0.0256 | [0.005, 0.132] | FAIL |
| OOD | unscorable = win | 40 | 2 | 0 | 38 | 0.0500 | [0.014, 0.165] | FAIL |
| OOD | unscorable = loss | 40 | 1 | 0 | 39 | 0.0250 | [0.004, 0.129] | FAIL |
| OOD | unscorable = tie | 40 | 1 | 1 | 38 | 0.0375 | [0.009, 0.147] | FAIL |

The rule would NOT have fired on that run: every imputation fails decisively, so the 4 items it
could not read were not decision-relevant, and escalating would have been noise. Stated because a
rule whose only worked example is a trigger is a rule nobody can calibrate — this one is here for
the run where the decision does move, and it stays quiet otherwise.

## If this file cannot be read

Escalate. Do **not** fall back to writing your own judge prompt: that is precisely the state this
file replaced, and it is worse than a stopped run because the run continues and produces a number
that looks like the last one. The same failure already happened one stage upstream — the finetune
agent authored its own trainer on every run because the canonical script was IAM-unreadable, and
produced a working one twice and an `UnboundLocalError` once.
