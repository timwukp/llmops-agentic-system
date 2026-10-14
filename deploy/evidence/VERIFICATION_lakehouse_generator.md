# VERIFICATION — lakehouse source generator (Phase 1 PR-06), 2026-10-14

**What it is.** `pipeline/lakehouse/generate.py` builds the four Bronze sources of the research
report's §2 (customer-service conversations in Traditional Chinese and English, CRM and ERP change
events, web events) in the exact shapes of the tables `deploy/10_lakehouse.py` deployed (PR-04). It
plants the defects PR-07's Silver/Gold rules must catch, with their exact counts and ids, in
`expected_defects.json`.

## Determinism across machines

The same seed was generated on two machines, and the `dataset_sha256` values were compared. That
value is the sha256 over every file's sha256: each file is written canonically, with sorted keys,
no whitespace and UTF-8.

| Scale | macOS laptop, Python 3.12.12 | build runner (AL2023), Python 3.12.14 |
|---|---|---|
| demo, seed 7 | `7a05b9ad1d089f1c398e2351e9ecd2aa821c63936aef8cf4e5f525edcf31971e` | identical |
| small, seed 7 | `a11d0272b84a2d383141595ed8f6224f96edcdae250b06c1f35444f37b59e5b2` | identical |

The determinism comes from a single seeded `random.Random`, ids from `uuid5`, and timestamps from a
fixed base date. Nothing reads the clock, the hostname or the environment.

## What the demo scale contains (seed 7)

| Table | Rows |
|---|---|
| `bronze.conversation_events` | 924 (300 conversations; 604 zh-TW / 320 EN turns; 12 of the rows are resends) |
| `bronze.crm_cdc` | 825 (customers c/u/d, tickets c/u) |
| `bronze.erp_cdc` | 1350 (orders c/u, inventory c) |
| `bronze.web_events` | 2000 |
| eval prompts (held out) | 40 |

| Planted defect | Count | Rule PR-07 must implement |
|---|---|---|
| PII in a customer turn (name, mobile, email, national ID, card) | 30 | detect + redact (S4's regex + Guardrails layering) |
| toxic customer turn | 6 | flag / drop |
| leaked `<think>` in an agent turn | 10 | strip (and a Gold DQ rule: no `<think>` in `output`) |
| out-of-order ingestion | 8 | order by `turn_idx`, not by `ingest_ts` |
| near-duplicate conversation | 6 | near-dup removal |
| eval-set contamination | 5 | drop rows whose question is in the eval set |
| duplicate resend (same `event_id`) | 12 | dedup on `event_id` |
| schema drift (`raw_meta.channel_detail` appears mid-stream) | 172 events | tolerate the new field |

## Offline proof

`tests/test_lakehouse_generate.py` has 42 tests, each run at both scales. Every planted defect is
re-detected by an **independent oracle**, written in the test, that never reads
`expected_defects.json`. The oracle's findings and the plant must agree exactly, in both directions:
a planted defect it cannot find fails, and a defect found where nothing was planted (for example a
phone number in a "clean" conversation) fails too. Every row is also checked against PR-04's deployed
schema.

Writing the oracles found one real generator defect. Promo and stock questions carry no order id, so
the same product and template repeated **verbatim**. Those were exact duplicates nobody planted, which
PR-07's dedup rule would have been blamed for. The agent's first reply now carries a case reference,
so every clean conversation is unique.

Three mutations of the generator were each caught: a clean turn leaking a phone number, an
out-of-order plant recorded but not applied, and a resend that is not byte-identical.

## Not covered here

This change lands no data in AWS. Loading these files into Bronze is PR-07's Glue job, and running
the generator as a pipeline step is PR-09. Conversations are built from templates, not written by
the teacher model: templates are what make every defect countable. A teacher paraphrase pass, if one
is wanted, would run through the pipeline.
