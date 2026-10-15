# Spec: Gold export

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. `pipeline/lakehouse/export.py`: `unload_sql` (int snapshot, Gold split tables only, quote-free
   prefix), `rows_from_unload`, canonical `to_jsonl` (sorted by pair_id), `sha256`,
   `manifest_insert_sql` (escaped strings, validated numbers), `extract_prefix`.
2. Glue Python-shell job `llmops-lakehouse-export`: snapshot ids from the Gold report, UNLOAD per split,
   `train.jsonl`/`val.jsonl`/`extract.json` under `lakehouse/extracts/<dataset>/<snapshot>/`, rows in
   `gold.dataset_manifest`, an export report.
3. Driver `_verify_gold_extract` on `extract_uri` + `extract_sha256`: reads only
   `s3://<DATA_BUCKET>/lakehouse/extracts/`, recomputes sha256 and rows, appends
   `attestations[kind=gold_extract]`; one stage may carry several attestations.
4. Schema: the gold_extract fields. Macie: `ensure_pii_scan` by prefix, a job of its own for extracts.
5. Console Data tab shows the export; `--smoke` exports twice and runs the driver check.
