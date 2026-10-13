# SPIKE_04_s4_pii_recall_zhtw — PII detection recall on Traditional Chinese and English text

- **Verdict:** PASS
- **Gates:** Phase 1 PR-07: the PII layering in Bronze -> Silver
- **Region:** us-east-1
- **Ran:** 2026-10-13T07:23:05Z → 2026-10-13T07:32:59Z

## Pass criteria

| id | criterion | result | detail |
|---|---|---|---|
| structured | A layered combination reaches recall >= 0.99 on structured types | **PASS** | guardrail+regex: 1.0 |
| names | Recall >= 0.95 on Chinese and English names | **PASS** | guardrail+regex: 0.999 |
| precision | Precision >= 0.9 | **PASS** | guardrail+regex: 1.0 |
| over_redaction | The over-redaction rate is measured per detector | **PASS** | regex=0.0, guardrail=0.0, glue=0.0 |

## Observations

- **records:** {"n": 1000, "seed": 20261013, "entities": 3523}
- **guardrail_source:** INPUT
- **glue_job_state:** SUCCEEDED
- **privacy_filter:** NOT RUN: needs the model mirrored via the pipeline's mirror_model task first; follow-up
- **detector_regex:** {"recall_structured": 1.0, "recall_names": 0.0, "recall_addresses": 0.0, "precision": 1.0, "over_redaction": 0.0, "detections": 2020}
- **detector_regex_by_type:** {"ADDRESS_EN/en": 0.0, "ADDRESS_ZH/zh-TW": 0.0, "CARD/en": 1.0, "CARD/zh-TW": 1.0, "EMAIL/en": 1.0, "EMAIL/zh-TW": 1.0, "NAME_EN/en": 0.0, "NAME_ZH/en": 0.0, "NAME_ZH/zh-TW": 0.0, "TW_ID/en": 1.0, "TW_ID/zh-TW": 1.0, "TW_LANDLINE/en": 1.0, "TW_LANDLINE/zh-TW": 1.0, "TW_MOBILE/en": 1.0, "TW_MOBILE/zh-TW": 1.0, "TW_UBN/en": 1.0, "TW_UBN/zh-TW": 1.0}
- **detector_guardrail:** {"recall_structured": 0.6797, "recall_names": 0.999, "recall_addresses": 1.0, "precision": 1.0, "over_redaction": 0.0, "detections": 2875}
- **detector_guardrail_by_type:** {"ADDRESS_EN/en": 1.0, "ADDRESS_ZH/zh-TW": 1.0, "CARD/en": 1.0, "CARD/zh-TW": 1.0, "EMAIL/en": 1.0, "EMAIL/zh-TW": 1.0, "NAME_EN/en": 1.0, "NAME_ZH/en": 0.9932, "NAME_ZH/zh-TW": 1.0, "TW_ID/en": 0.0, "TW_ID/zh-TW": 0.0, "TW_LANDLINE/en": 1.0, "TW_LANDLINE/zh-TW": 1.0, "TW_MOBILE/en": 0.9938, "TW_MOBILE/zh-TW": 0.9944, "TW_UBN/en": 0.0, "TW_UBN/zh-TW": 0.0}
- **detector_glue:** {"recall_structured": 0.3446, "recall_names": 0.352, "recall_addresses": 0.4274, "precision": 1.0, "over_redaction": 0.0, "detections": 1350}
- **detector_glue_by_type:** {"ADDRESS_EN/en": 0.8113, "ADDRESS_ZH/zh-TW": 0.0, "CARD/en": 1.0, "CARD/zh-TW": 0.0, "EMAIL/en": 1.0, "EMAIL/zh-TW": 0.3905, "NAME_EN/en": 1.0, "NAME_ZH/en": 0.0, "NAME_ZH/zh-TW": 0.0, "TW_ID/en": 1.0, "TW_ID/zh-TW": 0.0, "TW_LANDLINE/en": 0.0, "TW_LANDLINE/zh-TW": 0.0, "TW_MOBILE/en": 0.0, "TW_MOBILE/zh-TW": 0.0, "TW_UBN/en": 0.0, "TW_UBN/zh-TW": 0.8611}
- **best_combination:** guardrail+regex
- **best_combination_scores:** {"recall_structured": 1.0, "recall_names": 0.999, "recall_addresses": 1.0, "precision": 1.0, "over_redaction": 0.0, "detections": 4895}

## Errors

- none

## Teardown

- all tracked resources deleted

## Run history (2026-10-13)

| Run | Started (UTC) | Outcome | What it established |
|---|---|---|---|
| 1 | 07:12:02 | structured PASS, names FAIL, over_redaction FAIL | The Bedrock guardrail reported **zero** detections for every type, even English emails. Root cause, isolated with a one-record probe: a guardrail whose PII entities set only `action: ANONYMIZE` does **nothing** under `ApplyGuardrail(source="INPUT")` (response `action: NONE`, no `sensitiveInformationPolicy`), while `source="OUTPUT"` anonymized the same text. The API has separate `inputAction` / `outputAction` / `inputEnabled` / `outputEnabled`. |
| 2 | after the fix | **all 4 criteria PASS** | The table above. Every PII entity now sets both directions explicitly; detection then works under `source="INPUT"` (recorded as `guardrail_source`). |

## Consequences for Phase 1 (PR-07) and Phase 2

- **PR-07 layering: zh-TW regex layer + Bedrock Guardrails.** The regex layer (checksum-validated
  national ID and 統一編號, Luhn-validated cards, three mobile formats, landlines, email) carries the
  Taiwan-specific identifiers that Guardrails has no type for (TW_ID and TW_UBN recall 0.0). Guardrails
  carries names and addresses in both languages (recall 0.999 / 1.0), which no pattern can.
- **Not Glue Detect-PII for this data.** On zh-TW text it scored 0.0 for names, IDs, cards, phones and
  addresses (its PHONE_NUMBER covers US/Canada only), while the same entities in English text scored
  well (names 1.0, IDs 1.0, cards 1.0). The most likely cause is word-boundary tokenization: CJK text
  has no spaces around the entity. It can still be useful on English-only columns.
- **Every guardrail this platform creates must set `inputAction`/`inputEnabled` explicitly.** This
  matters again in Phase 2: a gateway that relies on `action` alone to anonymize user *input* would
  silently pass PII through.

## Limitations (stated, not assumed away)

- **Synthetic, template-built text.** Entities follow cue phrases (我是…, 手機…, "card number …"), which
  is probably easier than real conversations. Real Bronze data must be re-measured with a planted
  canary set before the layering is trusted in production.
- **Precision is measured against planted entities only.** 1.0 means nothing outside the planted spans
  was flagged in template text; it does not bound false positives on free-form conversation.
- **privacy-filter was not run** (it needs the model mirrored through the pipeline first).
- Records were generated at run time and deleted with `spikes/s4/`; only aggregates are committed.

