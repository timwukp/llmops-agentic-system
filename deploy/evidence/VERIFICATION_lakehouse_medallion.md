# VERIFICATION — lakehouse medallion (Phase 1 PR-07), 2026-10-14

**What ran.** `python deploy/10_lakehouse.py --region us-east-1 --smoke --seed 7 --scale <small|demo>`,
against the jobs and grants this change deploys. The flow:

1. generate the dataset (PR-06) and upload it to `s3://<DATA_BUCKET>/lakehouse/landing/<dataset>/`;
2. run the three Glue 5 jobs as `llmops-lakehouse-glue`:
   - `llmops-lakehouse-land-bronze`: landing → Bronze;
   - `llmops-lakehouse-bronze-to-silver`: the rules, with names checked through the Bedrock guardrail
     `llmops-lakehouse-pii` v1;
   - `llmops-lakehouse-silver-to-gold`;
3. hold the Silver rule report to the generator's `expected_defects.json` **exactly**;
4. read the Glue Data Quality outcomes, then count each table through Athena.

## Result (final code): 16 / 16 checks at each scale

| Check | small (seed 7) | demo (seed 7) |
|---|---|---|
| three jobs SUCCEEDED as the Glue role | PASS | PASS |
| `dedup_event_id` = planted resends | PASS (5) | PASS (12) |
| `tolerate_drift` = planted drift events | PASS (62) | PASS (172) |
| `order_by_turn` = planted out-of-order conversations | PASS (3) | PASS (8) |
| `strip_think` = planted `<think>` leaks | PASS (4) | PASS (10) |
| `drop_toxic` = planted toxic turns | PASS (3) | PASS (6) |
| `redact_pii` = planted PII, **names via the live guardrail** | PASS (10) | PASS (30) |
| `decontaminate` = planted eval contamination | PASS (3) | PASS (5) |
| `near_dup`: exactly one side of each planted pair | PASS (3) | PASS (6) |
| Silver DQDL (completeness, `Uniqueness pair_id = 1.0`, no `<think>`) | PASS | PASS |
| Gold DQDL (both tables) | PASS | PASS |
| Athena row counts = the reports (silver, gold train, gold eval) | PASS | PASS |
| Silver pairs / Gold train / Gold eval | 71 / 61 / 10 | 283 / 251 / 32 |

**Every PASS means exact equality with the plant, in both directions.** A rule that also catches
something unplanted fails the same way as one that misses a plant.

This is also the real write that PR-04 deferred. PR-04 proved the Glue role's grants only by
simulation; here the role landed, transformed and overwrote the Bronze, Silver and Gold tables
through the `s3tablescatalog` federation under SSE-KMS.

## What the live runs found

1. **The SDK in Glue 5 is too old for Guardrails.** Glue 5 bundles a boto3 whose `bedrock-runtime`
   client has no `apply_guardrail` (first small run: `AttributeError`). The Bronze→Silver job now
   installs the repo's SDK floor through `--additional-python-modules boto3==1.43.57,botocore==1.43.57`,
   and a test pins that.
2. **Guardrails reads Taiwanese store names as people's names.** The first demo-scale run caught
   **36** PII conversations against **30** planted. The 6 extra were all stock questions naming a
   store: `高雄前鎮店` (a Kaohsiung district) and `Taichung Xitun`, which the guardrail returned as
   `NAME`. Spike S4's dataset had no organisation names in it, so its 1.0 precision never met this
   case. The fix is a reference list of organisation-owned entities
   (`pipeline/lakehouse/reference/org_entities.json`: the retailer's stores and products in both
   languages). A detected name is not redacted when it lies inside one of those entities in the same
   text. The rerun was exact. A test requires every store and product the generator writes to be in
   the list, and another shows the list only protects text where the entity actually appears.
3. **Near-duplicates need a business key.** On the demo data, a planted near-duplicate can score as
   low as 0.878 character-trigram Jaccard, while *different* clean conversations from the same
   template reach 0.945, so no similarity threshold separates them. The rule therefore groups on the
   case reference first and compares questions second. It uses `difflib`'s sequence ratio: a
   one-character change in a short Chinese question (請問→請教) drops trigram Jaccard to 0.667.
   Reducing template-level redundancy (MinHash-style diversity filtering) is a separate quality
   decision and is not made here.

## Offline proof

- **`tests/test_lakehouse_transform.py`:** every rule equals its plant, in both directions, for 3
  seeds × 2 scales. It also covers the Silver and Gold schema against the deployed tables, the
  deterministic split, the `messages` mapping, the structured PII layer (checksum lookalikes are left
  alone), the org-entity list, and `compare_to_plants`.
- **Mutations of the rules:** four (a disabled ID checksum, an over-broad toxic lexicon, too strict a
  near-dup threshold, decontamination switched off), each caught.
- **`tests/test_lakehouse_storage.py`:**
  - the guardrail is created with both directions enabled, published as a numbered version, and
    reused on redeploy;
  - the three jobs and their uploaded scripts;
  - every new Glue role grant is scoped to its prefix, and `ApplyGuardrail` to the one guardrail;
  - the SDK pin.
- **`--verify` after deploy:** 41 checks, all PASS. The Glue role was checked with
  `simulate_principal_policy` in both directions: landing read, reports write and the guardrail are
  allowed; landing write and other guardrails are denied.

## Not covered here

- **Scale.** The jobs collect a table to the Spark driver so they can call the pure functions. That
  is right for the demo's hundreds of rows, not for millions; distributing them is a
  `mapInPandas` change.
- **Pipeline integration.** Running this from the pipeline is PR-09.
- **Gold export.** `UNLOAD` with sha256 is PR-08.
