# r6c 8B scaling diagnosis — the acceptance set asks for facts decontamination deletes

Measured 2026-08-12 from r6c's own surviving artifacts (`run-20260811T165529Z-ce628817`), which
completed data-prep, an 8B QLoRA fine-tune and full inference before dying at EvalScore in a
Bedrock outage. The 137 student answers survived in S3, so this diagnosis cost **$9.08 of judge
tokens and no GPU time** (170,249 input + 86,981 output tokens, Opus 5 list price).

Reproduce: `offline_judge.py` in the r6-relaunch working dir; raw judgments in
`r6c_judged.jsonl`, aggregate in `r6c_offline_report.json`.

## 1. The headline: an 8B student decisively FAILS the reformed bar

Judged under the #89 metric — `judge_score = (wins + 0.5*ties)/n`, blinded pairwise against the
customer's own reference resolutions, **every item judged in BOTH positions**, Wilson 95%:

| layer | n | W | T | L | judge_score | Wilson 95% | vs bar 0.45 |
|---|---|---|---|---|---|---|---|
| in-distribution (`id`) | 94 | 3 | 36 | 55 | **0.2234** | [0.151, 0.318] | **FAIL** (upper < bar) |
| out-of-distribution (`ood`) | 39 | 1 | 0 | 38 | **0.0256** | [0.005, 0.132] | report-only, never gated |

Unscorable and reported, not scored: `id#79`, `id#86`, `id#93`, `ood#2` (see §4).

**Scaling bought almost nothing where it was supposed to.** r5's 1.7B student scored 0.0 on the
40-row set that is now the OOD layer; r6c's 8B scores 0.0256 on that same set — one win in 39.
The entire visible gain is on the in-distribution layer, and 0.223 is not within reach of 0.45:
the interval excludes the bar by 13 points. The r6 research pass refuted the published
"a bigger student closes this gap" claims 0-3 on the literature; this is the first time we have
measured it on our own data, at 8B, and the measurement agrees with the refutation.

## 2. Why it loses: invented org-specific facts, not size, style or verbosity

The judge's stated reasoning on losses is consistent — the student is fluent, well-structured and
confidently wrong on specifics, while the reference wins on correct specifics plus concrete
timelines and fallbacks. Verbatim examples:

- `id#67` (password): student says the lockout "resets automatically at midnight local time";
  judge calls that "atypical for account lockout policies" and notes it leaves the user blocked.
- `id#27` (email): student invokes a "nonexistent 'autodiscover port'" and conflates home vs
  corporate networks, after getting the core fix right.
- `id#82` (onboarding): student "hides behind invented gatekeeping ('Legal Hardware Pool',
  'IT-owned entitlement')" with no timeline and no contingency.
- `ood#23` (teams): student conflates private-channel invite semantics with cross-tenant
  shared-channel/guest access.

Two candidate explanations are **excluded by measurement**, not by argument:

- **Not verbosity.** Student/reference character ratio is 0.905 (ID) and 0.849 (OOD). r5's 1.7B
  ran 0.77-0.80, so the length handicap the length-control work was designed for has largely
  closed on its own, and the score did not follow.
- **Not position bias.** Both positions were judged for every item: only 6 of 94 ID items
  (6.4%) and 0 of 39 OOD items flipped with position.

## 3. The main contradiction: correct decontamination deletes the answers

From r6c's own `distillation/stats.json`:

```
300 input rows
 -39 exact duplicates
 -33 near duplicates                      -> 228
 -94 dropped: prompt trigram-Jaccard >= 0.6 against the 97-row ID acceptance set   -> 134
 -13 quality score < 4                    -> 121 output rows  (107 train / 14 val)
post-decontamination max Jaccard vs eval prompts: 0.586
```

**94 of the 228 surviving rows — 41% — were deleted precisely because they resembled the
acceptance questions.** High prompt overlap means a near-identical ticket, and a near-identical
ticket's resolution *is* the answer being asked for. So the holdout is scientifically correct and
structurally lethal at the same time: it guarantees the student never saw the facts, and then the
gate scores the student against a human reference that states them.

The per-category breakdown adds one thing and **cannot** add another, and the difference matters:

- What it shows: within the 12 categories the 107 training rows cover, scores range from **0.000
  to 0.625** — `software` 0.625, `vpn` and `onboarding` 0.375, down to `printer` 0.000 (n=9) and
  `mfa` 0.000 (n=7). So being represented in training is **not sufficient**: two trained
  categories score zero. Coverage alone does not predict success.
- What it cannot show: the trained/absent split is **exactly** the ID/OOD split — all 97 ID items
  are in trained categories, all 40 OOD items are in absent ones (verified). The two variables are
  perfectly collinear here, so the ID-vs-OOD gap **cannot be attributed to category coverage
  rather than to distribution shift** from this data. Anyone reading the 0.223-vs-0.026 contrast as
  "because those categories were missing from training" is reading a confound. Separating them
  needs a set that holds one variable fixed — a deliberate r7 measurement, not an inference here.
- One counterexample worth keeping honest: of the 23 categories absent from training, 22 score
  exactly 0.000; `training` (n=2) scores 0.500. At n=2 that is noise, but it is not zero, and the
  sweeping version of this claim is false.

The 36 ID ties (38% of the layer) are the items where general procedure was enough.

**No student size fixes the missing facts.** The information is not in the training set, by
construction — that part follows from the arithmetic above, not from the category table.

## 4. The judge itself gets content_filtered — 3.3% of judgment slots

**9 of the 274 (item, position) slots — 3.3%, spanning 7 distinct items — returned
`stopReason: content_filtered` with an empty text block** at least once (19 filtered records in
all, since a filtered slot was retried). The filter is not deterministic: retries recovered 5 of
the 9 slots, and after retries **4 items remain unjudgeable**: `id#79`, `id#86`, `id#93`,
`ood#2`. The affected tickets
cluster on credential/lockout/MFA/access content (`vpn`, `mfa` x2, `access`, `laptop`,
`software`, `mobile`), i.e. exactly the material a helpdesk corpus is made of.

This is the same failure mode that killed r5's EvalGate, reaching the *measuring instrument*
rather than the agent. In-run it is worse than a crash, because the honest outcome is a silently
smaller `n`: an item nobody could judge must be reported as unscorable, never folded into the
denominator (which would depress the score with the judge's silence) and never folded into
"tie" (which would invent a verdict). The eval prompt should say so explicitly.

## 5. Also found: the instrument is re-authored every run

The mirrored `llm-evaluation` skill implements only a 1-5 absolute score
(`compute_llm_judge`); the blinded pairwise judge is written fresh by the eval agent on every
run. A scaling experiment whose instrument changes between data points is not strictly
comparable run-to-run — the same defect the training script had, and the same cure applies:
pin the prompt in-repo and mirror it to S3 alongside `code/distill/train_qlora.py`.

Corollary already confirmed: **r5's `judge_ties: 0` was an artifact of its agent-written prompt
offering only A or B**, not a measurement. Offered the option, this judge ties on 38% of
in-distribution items.

## 5b. Two defects in this analysis's own first pass, and why the numbers above are not theirs

Recorded because both would have produced a plausible wrong answer, and because both are the
kind of thing an in-run agent would hit identically:

1. **The judge reasons before answering by default, and reasoning is billed as output tokens.**
   At `maxTokens: 400` the hard items — precisely the ambiguous ones a tie exists for — spent the
   entire budget inside a `reasoningContent` block and returned an EMPTY text block with
   `stopReason: max_tokens`. The first pass recorded 30 of 274 (11%) as "unparsed". A truncated
   judge is not an undecided judge: the budget was raised to 2000 and every row now records its
   stop reason, so truncation can never again be read as indecision. Had those 30 been scored as
   losses or ties, the ID score would have moved without any change in the student.
2. **`idx` restarts at 0 in every layer**, so 40 of the 137 items shared an `idx` with another
   item (ID 0..96, OOD 0..39). Keying judgments on `idx` alone cross-paired an ID item's
   A-position verdict with an OOD item's B-position verdict and silently dropped 40 ID items out
   of their layer — the first aggregate reported `items_in_layer: 57` for a 97-item layer. Keying
   on `layer#idx` fixed it without re-billing a single call, because every judgment had been made
   against the correct content; only the bookkeeping was wrong.

The tell in both cases was a count that did not reconcile with a number known independently
(274 vs 244 settled; 57 vs 97 items), which is the cheapest audit available and is why the
aggregate reports `items_in_layer` and `unscorable_items` at all.

## 6. What this means for the plan (decisions are the user's)

Running 1.7B and 4B through the full pipeline to complete the scaling table would now mostly
re-measure a conclusion already in hand: the gap to 0.45 is not a size gap. Four directions,
cheapest first:

1. **Gate on what distillation can actually deliver.** Score procedural correctness, safety and
   format against a rubric, and report fact-accuracy vs the customer's resolutions separately.
   Cheapest to test (prompt + plan change only) and it is the honest description of the product.
2. **Make the fact space learnable.** Extract a fact sheet from the customer's resolutions and
   have data-prep synthesize coverage from it, so a fact appears in many clusters and the holdout
   removes one instance rather than the knowledge. Needs a data-prep change, no new services.
3. **Give the student the facts at inference** (retrieval over the customer's KB). This changes
   the deliverable from "a distilled model" to "a distilled model plus retrieval" — the biggest
   platform change of the four, and the only one that addresses OOD categories at all.
4. **Raise the ceiling on the training set itself.** 300 tickets in, 107 rows trained on. If the
   customer can supply more history, everything above gets easier.

Regardless of which is chosen, three defects found here are worth fixing on their own merits:
the judge prompt should be pinned (§5), content_filtered judgments must be reported as
unscorable rather than silently shrinking `n` (§4), and an `eval_only` pipeline entry would make
offline re-judging of surviving artifacts a first-class platform action instead of a script in a
working directory (the state machine currently has only `full` and `data_audit`, and no Choice
state reads stage status, so r6c's paid 12.2 GiB model cannot be re-entered at all).
