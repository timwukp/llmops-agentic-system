# VERIFICATION — console Data tab, read-only (2026-10-14)

**What was deployed.** The console was deployed with `deploy/console/deploy.sh` (Lambda `llmops-admin`,
vendored boto3 1.43.113) and the lakehouse jobs with `deploy/10_lakehouse.py`. Their reports now carry
row counts, current snapshot ids and the plant comparison. A demo-scale smoke
(`--smoke --seed 7 --scale demo`, 16/16 PASS) then produced the dataset the tab shows.

## Live, in the portal (`/?tab=data`)

| Panel | Shown | Source |
|---|---|---|
| Dataset picker | 5 datasets, newest first | `lakehouse/reports/<dataset>/` prefixes |
| Header | dataset id, seed 7, demo, sha256 `7a05b9ad…`, stages land / silver / gold | landing `manifest.json`, which reports exist |
| Silver pairs | 283 | `silver.json` |
| Gold train / eval | 251 / 32 | `gold.json` |
| Rules vs plants | **8/8** | `silver.json` `plant_check` |
| Data quality | **16/16** | Glue DQ outcomes in `silver.json`, `gold.json` |
| Tables | all 10, with rows and snapshot for the 7 written (924 / 825 / 1.4K / 2.0K Bronze, 283 Silver, 251 / 32 Gold) | S3 Tables `ListTables` + the reports |
| Rules · planted vs caught | 8 rows, each `exact` | `plant_check` |
| Data quality outcomes | 16 rules, each `Passed` | the reports |
| Exports · Lineage | placeholders: PR-08, PR-05 | — |

Checked through the live API from the page:
- `GET /api/data/run?dataset=../../customer-data` → **400**, before any S3 read.
- An older dataset, whose reports predate the new fields, loads with `plant_check: null`, the land stage
  marked missing and no table rows.

## Found by checking the live page

Switching from a new dataset to an old one left the previous dataset's rows and snapshots in the
Tables panel, because only the cells present in the new report were overwritten. The loader now resets
every cell first. The Rules card's wording for a missing comparison also said "real data", which was
inaccurate for an older run. Both were fixed, redeployed and re-checked: the old dataset shows `–` and
`n/a`, and the new one 924 and 8/8.

## Least privilege

- **The tab never reads the tables.** The tables are SSE-KMS and Bronze holds PII; a console role that
  could decrypt them would be one more path to that PII. Every number on the tab comes from a report
  the Glue jobs wrote.
- **One new grant, `LakehouseTablesMetadata`:** `s3tables:ListTables`, `ListNamespaces`, `GetTable` and
  `GetTableBucket` on the two lakehouse buckets only. No `GetTableData`, no KMS.
  `tests/test_console_data.py` asserts both the grant and the absence of any table-data or decrypt call
  in the tab's code.
- **The dataset id is regex-checked** (`^[a-z0-9][a-z0-9-]{0,79}$`) before it is used in an S3 key.

The console's pre-existing `S3Manifests` statement (`s3:GetObject`/`ListBucket` on `*`) is broader
than it needs to be. It predates this change and is tracked separately; it is not widened here.

## Also in this change

- `deploy/console/deploy.sh` writes its scratch files under `$TMPDIR` when one is set, and `/tmp`
  otherwise. In a sandboxed shell the old hard-coded `/tmp` paths failed.
